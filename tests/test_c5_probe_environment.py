"""C5-R1a: adapter resolution and the default container probe environment."""

from pathlib import Path

import pytest

from test_c5_doctor_probe import VERSION_CLI, container, probe, project, s


@pytest.mark.parametrize("versioned", [False, True])
def test_adapter_probe_uses_native_resolution_with_provider_path(
        tmp_path, monkeypatch, container, versioned):
    tools = tmp_path / "tools"
    tools.mkdir()
    if versioned:
        target = s.write_cli(tmp_path / "versions", name="1.2.3", body=VERSION_CLI)
        (tools / "fakecli").symlink_to(target)
        container.mount(tmp_path / "versions")
    else:
        target = s.write_cli(tools, body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch, entry={
        "bin": "fakecli", "adapter": "fake-adapter.sh",
        "env": {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tmp_path / "run-home")},
    })
    container.mount(tools)

    result = probe(paths)

    assert result.version == "1.2.3"
    (argv, env), = container.calls
    assert argv == [str(target), "--version"]
    assert env["PATH"] == f"{tools}:/usr/bin:/bin"
    assert env["HOME"] == str(Path.home())


def test_direct_probe_passes_launch_path_and_default_home(tmp_path, monkeypatch, container):
    launcher = s.write_cli(tmp_path / "tools", body=VERSION_CLI)
    paths = project(tmp_path, monkeypatch, entry={
        "bin": str(launcher), "env": {"PATH": "/special/bin:/usr/bin:/bin"},
    })
    container.mount(launcher)

    assert probe(paths).version == "1.2.3"
    (_, env), = container.calls
    assert env["PATH"] == "/special/bin:/usr/bin:/bin"
    assert env["HOME"] == str(Path.home())
