"""TS-R1/R2, review ag-dc89fc: conftest's host-CLI guard decides on the binary
the shipped script will actually run.

That is MULTIAGENTS_BIN after the provider's own `env:` and the caller's
`extra_env`, not the provider's `bin`. A "host" CLI is one outside the pytest
basetemp; each fake CLI only records that it ran.
"""
from __future__ import annotations

import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from multiagents import scripts
from multiagents.executor.local import LocalExecutor
from multiagents.providers import Provider


def _cli(directory: Path, marker: Path) -> Path:
    cli = directory / "agy"
    cli.write_text(f"#!{sys.executable}\n"
                   f"open({str(marker)!r}, 'a').write('ran\\n')\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return cli


@pytest.fixture
def host(tmp_path):
    """A CLI outside the basetemp, standing in for the developer's own."""
    directory = Path(tempfile.mkdtemp(prefix="ts-host-cli-"))
    try:
        yield _cli(directory, tmp_path / "host-ran")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def local(tmp_path):
    (tmp_path / "local").mkdir()
    return _cli(tmp_path / "local", tmp_path / "local-ran")


def _budget(tmp_path, provider, **kwargs):
    return scripts.run_action("agy", provider, LocalExecutor(), "budget", tmp_path,
                              timeout=20, **kwargs)


def test_a_provider_env_pointing_at_a_host_cli_is_refused(tmp_path, host, local):
    provider = Provider.from_dict("agy", {"bin": str(local),
                                          "env": {"MULTIAGENTS_BIN": str(host)}})
    _budget(tmp_path, provider)
    assert not (tmp_path / "host-ran").exists(), "the host CLI ran"
    assert not (tmp_path / "local-ran").exists()


def test_extra_env_pointing_at_a_host_cli_is_refused(tmp_path, host, local):
    provider = Provider.from_dict("agy", {"bin": str(local)})
    _budget(tmp_path, provider, extra_env={"MULTIAGENTS_BIN": str(host)})
    assert not (tmp_path / "host-ran").exists(), "the host CLI ran"


def test_a_provider_env_pointing_at_a_test_cli_runs_it(tmp_path, host, local):
    provider = Provider.from_dict("agy", {"bin": str(host),
                                          "env": {"MULTIAGENTS_BIN": str(local)}})
    _budget(tmp_path, provider)
    assert (tmp_path / "local-ran").exists(), "the test's own CLI was suppressed"
    assert not (tmp_path / "host-ran").exists()


def test_a_bin_outside_the_basetemp_is_still_refused(tmp_path, host):
    _budget(tmp_path, Provider.from_dict("agy", {"bin": str(host)}))
    assert not (tmp_path / "host-ran").exists(), "the host CLI ran"


def test_a_bin_under_the_basetemp_still_runs(tmp_path, local):
    _budget(tmp_path, Provider.from_dict("agy", {"bin": str(local)}))
    assert (tmp_path / "local-ran").exists(), "the test's own CLI was suppressed"


# Review ag-7236bf: a relative binary, or a bare name found through a relative
# PATH entry, is resolved against the action's working directory.

def test_a_relative_bin_resolved_from_the_actions_cwd_is_refused(tmp_path, host):
    provider = Provider.from_dict("agy", {"bin": str(host)})
    _budget(tmp_path, provider, cwd=host.parent.parent,
            extra_env={"MULTIAGENTS_BIN": f"{host.parent.name}/agy"})
    assert not (tmp_path / "host-ran").exists(), "the host CLI ran"


def test_a_bare_name_on_a_relative_path_entry_is_refused(tmp_path, host):
    provider = Provider.from_dict("agy", {"bin": str(host)})
    _budget(tmp_path, provider, cwd=host.parent.parent,
            extra_env={"MULTIAGENTS_BIN": "agy",
                       "PATH": f"{host.parent.name}:/usr/bin:/bin"})
    assert not (tmp_path / "host-ran").exists(), "the host CLI ran"


def test_a_relative_bin_under_the_basetemp_still_runs(tmp_path, host, local):
    provider = Provider.from_dict("agy", {"bin": str(host)})
    _budget(tmp_path, provider, cwd=tmp_path, extra_env={"MULTIAGENTS_BIN": "local/agy"})
    assert (tmp_path / "local-ran").exists(), "the test's own CLI was suppressed"
    assert not (tmp_path / "host-ran").exists()
