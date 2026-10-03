"""C5: `doctor`'s docker CLI probe resolves binaries the way a docker spawn does
(context/specs/phase6-closing-fixes.md, C5-R1, C5-R1a, C5-R2, C5-R3).

No docker is needed. `DockerExecutor.exec_in_running` is replaced by an emulated
container that RECORDS the argv and env it is handed and then behaves like a
container would: a program given by absolute path runs only if that path lies
under a mounted host directory; a bare name is looked up on the container's own
system PATH (which holds nothing of ours); anything else exits 127 with no
stderr, as the real exec wrapper does. It never succeeds whatever the argv is.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import d3_support as s  # noqa: E402

VERSION_CLI = 'echo "fakecli 1.2.3"\n'


class Container:
    """The emulated project container."""

    def __init__(self):
        self.mounts: list[Path] = []
        self.calls: list[tuple[list[str], dict | None]] = []

    def mount(self, *paths: Path) -> None:
        self.mounts += [Path(p) for p in paths]

    def exec_in_running(self, argv, timeout, *, env=None):
        self.calls.append((list(argv), None if env is None else dict(env)))
        prog = argv[0]
        if "/" not in prog:
            return 127, "", ""            # the container's PATH has no such CLI
        path = Path(prog)
        if not any(m == path or m in path.parents for m in self.mounts):
            return 127, "", ""
        if not path.exists():
            return 127, "", ""
        run = subprocess.run([prog, *argv[1:]], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
        return run.returncode, run.stdout, run.stderr


@pytest.fixture
def container(monkeypatch):
    from multiagents.executor.docker import DockerExecutor
    box = Container()
    monkeypatch.setattr(DockerExecutor, "exec_in_running",
                        lambda self, argv, timeout, **kw: box.exec_in_running(argv, timeout, **kw))
    return box


def project(tmp_path, monkeypatch, *, entry, executor_cfg=None):
    project_yaml = {"executor": {"kind": "docker"}}
    if executor_cfg is not None:
        project_yaml["executor"]["docker"] = executor_cfg
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=None,
                                 extra_entry=entry, project_yaml=project_yaml)
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    # the host's PATH does not hold the CLI either
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return paths


def probe(paths, context="docker"):
    return s.api().probe("fakecli", paths, context)


def execed_programs(box):
    return [argv[0] for argv, _ in box.calls]


# ---------------------------------------------------------------- C5-R1 / R1a

def test_c5_r1_a_cli_found_through_bin_search_is_probed_by_its_resolved_path(
        tmp_path, monkeypatch, container):
    launcher = s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    container.mount(tmp_path / "tools")
    result = probe(paths)
    assert result.state != "missing", result
    assert result.version == "1.2.3"
    assert execed_programs(container) == [str(launcher)]


def test_c5_r1a_the_probe_keeps_the_version_command_after_the_resolved_program(
        tmp_path, monkeypatch, container):
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    container.mount(tmp_path / "tools")
    probe(paths)
    (argv, _), = container.calls
    assert argv[1:] == ["--version"]


def test_c5_r1a_a_versioned_symlink_is_probed_by_the_version_current_now(
        tmp_path, monkeypatch, container):
    versions = tmp_path / "versions"
    real = s.write_cli(versions, name="1.2.3", body=VERSION_CLI)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakecli").symlink_to(real)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(bindir)]})
    container.mount(versions, bindir)
    result = probe(paths)
    assert result.state != "missing", result
    # a docker launch names the resolved target, not the symlink
    assert execed_programs(container) == [str(real)]


def test_c5_r1a_a_repointed_version_symlink_is_followed(tmp_path, monkeypatch, container):
    versions = tmp_path / "versions"
    s.write_cli(versions, name="1.0.0", body='echo "fakecli 1.0.0"\n')
    new = s.write_cli(versions, name="2.0.0", body='echo "fakecli 2.0.0"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakecli").symlink_to(versions / "1.0.0")
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(bindir)]})
    container.mount(versions, bindir)
    (bindir / "fakecli").unlink()
    (bindir / "fakecli").symlink_to(new)
    assert probe(paths).version == "2.0.0"
    assert execed_programs(container) == [str(new)]


def test_c5_r1_a_bare_bin_that_is_on_no_path_anywhere_is_not_passed_bare(
        tmp_path, monkeypatch, container):
    """The container's PATH does not hold the CLI; the bare name must not reach exec."""
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    container.mount(tmp_path / "tools")
    probe(paths)
    assert "fakecli" not in execed_programs(container)


def test_c5_r1_the_doctor_row_reports_the_version_not_missing(
        tmp_path, monkeypatch, capsys, container):
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    container.mount(tmp_path / "tools")
    _, out = s.doctor(paths.root, capsys)
    rows = [l for l in s.section(out, "cli dependencies") if "fakecli" in l and "docker" in l]
    assert rows and all("missing" not in r for r in rows), out
    assert any("1.2.3" in r for r in rows), rows


def test_c5_r1a_without_mount_cli_from_host_the_bare_name_goes_to_the_container_path(
        tmp_path, monkeypatch, container):
    """Holds today and must keep holding: nothing is mounted, so a PATH lookup."""
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]},
                    executor_cfg={"mount_cli_from_host": False})
    container.mount(tmp_path / "tools")   # even if reachable, the host path must not be used
    result = probe(paths)
    assert execed_programs(container) == ["fakecli"]
    assert result.state == "missing"


# ------------------------------------------------------------------- C5-R2

def test_c5_r2_a_cli_absent_from_the_container_is_still_missing(
        tmp_path, monkeypatch, container):
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    # nothing mounted: the resolved path does not exist in the container
    assert probe(paths).state == "missing"


def test_c5_r2_the_missing_detail_names_the_path_that_was_tried(
        tmp_path, monkeypatch, container):
    launcher = s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    result = probe(paths)
    assert result.state == "missing"
    assert str(launcher) in result.detail, result.detail


def test_c5_r2_an_absolute_bin_missing_everywhere_is_missing_and_names_the_path(
        tmp_path, monkeypatch, container):
    ghost = tmp_path / "nowhere" / "fakecli"
    paths = project(tmp_path, monkeypatch, entry={"bin": str(ghost)})
    result = probe(paths)
    assert result.state == "missing"
    assert str(ghost) in result.detail, result.detail


def test_c5_r2_a_missing_container_cli_is_a_doctor_problem(
        tmp_path, monkeypatch, capsys, container):
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    _, out = s.doctor(paths.root, capsys)
    rows = [l for l in s.section(out, "cli dependencies") if "fakecli" in l and "docker" in l]
    assert any("missing" in r for r in rows), out


def test_c5_r1_a_cli_that_exits_nonzero_is_probe_failed_not_missing(
        tmp_path, monkeypatch, container):
    s.write_cli(tmp_path / "tools", body="exit 3\n")
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    container.mount(tmp_path / "tools")
    assert probe(paths).state == "probe_failed"


# ------------------------------------------------------------------- C5-R3

def test_c5_r3_an_absolute_bin_is_still_probed_and_reports_the_version(
        tmp_path, monkeypatch, container):
    cli = s.write_cli(tmp_path / "abs", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch, entry={"bin": str(cli)})
    container.mount(tmp_path / "abs")
    result = probe(paths)
    assert result.version == "1.2.3" and result.state != "missing"
    assert execed_programs(container) == [str(cli)]


def test_c5_r3_the_host_probe_is_unchanged_and_never_touches_the_container(
        tmp_path, monkeypatch, container):
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    result = probe(paths, "host")
    assert result.version == "1.2.3" and result.state != "missing"
    assert container.calls == []


def test_c5_r3_the_host_probe_still_reports_missing_with_the_searched_places(
        tmp_path, monkeypatch, container):
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    result = probe(paths, "host")
    assert result.state == "missing"
    assert str(tmp_path / "tools" / "fakecli") in result.detail
    assert container.calls == []


def test_c5_r1a_the_probe_never_starts_a_container_that_is_not_running(
        tmp_path, monkeypatch):
    """Real exec_in_running against a fake docker whose container is stopped."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_d3_manifest_doctor import FAKE_DOCKER, FORBIDDEN
    bindir = tmp_path / "dockerbin"
    s.write_cli(bindir, "docker", body=FAKE_DOCKER.split("\n", 1)[1])
    state = tmp_path / "dstate"
    state.mkdir()
    (state / "state").write_text("exited")
    monkeypatch.setenv("D3_DIR", str(state))
    s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch,
                    entry={"bin": "fakecli", "bin_search": [str(tmp_path / "tools")]})
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    assert probe(paths).state == "container not running"
    verbs = [l.split()[0] for l in (state / "log").read_text().splitlines() if l.strip()]
    assert not set(verbs) & FORBIDDEN and "exec" not in verbs
