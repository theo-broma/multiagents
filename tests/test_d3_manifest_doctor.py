"""D3 doctor section and the docker probe seam (DM-R6), plus DM-R7 (nothing
else changes). The docker side is driven with a fake `docker` executable on
PATH; it records every invocation so the tests can rule out starting,
creating or seeding a container.
"""

from __future__ import annotations

import re
import stat
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import d3_support as s  # noqa: E402

FAKE_DOCKER = r"""#!/bin/sh
# $D3_DIR/state: running|exited   $D3_DIR/exec: ok|hang|fail|missing
echo "$@" >> "$D3_DIR/log"
state=$(cat "$D3_DIR/state" 2>/dev/null || echo exited)
mode=$(cat "$D3_DIR/exec" 2>/dev/null || echo ok)
case "$1" in
  inspect)
    if [ "$state" != running ]; then
      case "$*" in *State.Status*) echo exited; exit 0 ;; *State.Running*) echo false; exit 0 ;; esac
      exit 1
    fi
    case "$*" in *State.Status*) echo running ;; *State.Running*) echo true ;; *) echo running ;; esac
    exit 0 ;;
  ps)
    [ "$state" = running ] && echo multiagents-test
    exit 0 ;;
  exec)
    case "$*" in *kill*) exit 0 ;; esac
    case "$mode" in
      hang) sleep 60; exit 0 ;;
      fail) echo "boom" >&2; exit 3 ;;
      missing) echo "not found" >&2; exit 127 ;;
      *) echo "fakecli 1.2.3"; echo "warn" >&2; exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
"""

FORBIDDEN = {"run", "create", "start", "restart", "build", "cp", "rm", "pull", "load",
             "network", "volume", "commit", "unpause", "update"}


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "dockerbin"
    s.write_cli(bindir, "docker", body=FAKE_DOCKER.split("\n", 1)[1])
    state = tmp_path / "dstate"
    state.mkdir()
    (state / "state").write_text("running")
    (state / "exec").write_text("ok")
    monkeypatch.setenv("D3_DIR", str(state))
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")

    class Fake:
        def set(self, state_=None, exec_=None):
            if state_:
                (state / "state").write_text(state_)
            if exec_:
                (state / "exec").write_text(exec_)

        def calls(self):
            log = state / "log"
            return [l.split() for l in log.read_text().splitlines()] if log.exists() else []

        def verbs(self):
            return [c[0] for c in self.calls() if c]

    return Fake()


def docker_executor(paths, gdir, provider="fakecli"):
    from multiagents.executor import get_executor
    from multiagents.providers import load_providers
    raw = yaml.safe_load((paths.config / "providers.yaml").read_text())["providers"]
    return get_executor("docker", {}, paths=paths, providers=load_providers(raw),
                        config_dir=gdir)


# ================================================== exec_in_running seam

@pytest.fixture
def dproj(tmp_path, monkeypatch, fake_docker):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    return paths, gdir


def test_dm_r6_seam_returns_rc_stdout_stderr_from_a_running_container(dproj, fake_docker):
    paths, gdir = dproj
    ex = docker_executor(paths, gdir)
    rc, out, err = ex.exec_in_running(["fakecli", "--version"], 10)
    assert (rc, out.strip(), err.strip()) == (0, "fakecli 1.2.3", "warn")


def test_dm_r6_seam_reports_a_nonzero_exit_status(dproj, fake_docker):
    paths, gdir = dproj
    fake_docker.set(exec_="fail")
    rc, out, err = docker_executor(paths, gdir).exec_in_running(["fakecli"], 10)
    assert rc == 3 and "boom" in err


@pytest.mark.parametrize("state", ["exited", "absent", "created", "paused"])
def test_dm_r6_seam_yields_not_running_and_never_starts_anything(dproj, fake_docker, state):
    paths, gdir = dproj
    fake_docker.set(state_=state)
    result = docker_executor(paths, gdir).exec_in_running(["fakecli", "--version"], 10)
    assert not isinstance(result, tuple)
    assert "NotRunning" in type(result).__name__ or "NotRunning" in repr(result)
    assert not set(fake_docker.verbs()) & FORBIDDEN, fake_docker.calls()
    assert "exec" not in fake_docker.verbs()      # nothing was run in a container that is not


def test_dm_r6_seam_never_creates_starts_or_seeds_even_when_running(dproj, fake_docker):
    paths, gdir = dproj
    docker_executor(paths, gdir).exec_in_running(["fakecli", "--version"], 10)
    assert not set(fake_docker.verbs()) & FORBIDDEN, fake_docker.calls()


def test_dm_r6_seam_timeout_returns_within_timeout_plus_5_and_kills_inside_the_container(
        dproj, fake_docker):
    paths, gdir = dproj
    fake_docker.set(exec_="hang")
    start = time.monotonic()
    result = docker_executor(paths, gdir).exec_in_running(["fakecli", "--version"], 2)
    elapsed = time.monotonic() - start
    assert elapsed < 2 + 5 + 1
    assert not (isinstance(result, tuple) and result[0] == 0)       # not a success
    execs = [c for c in fake_docker.calls() if c and c[0] == "exec"]
    assert any("kill" in " ".join(c) for c in execs[1:]), (
        "no container-side kill after the host-side exec timed out", fake_docker.calls())
    assert "kill" not in [c[0] for c in fake_docker.calls()]   # `docker kill` would stop the container


def test_dm_r6_seam_gives_the_process_no_tty_and_no_stdin(dproj, fake_docker):
    paths, gdir = dproj
    docker_executor(paths, gdir).exec_in_running(["fakecli", "--version"], 10)
    (call,) = [c for c in fake_docker.calls() if c and c[0] == "exec"]
    flags = [a for a in call[1:] if a.startswith("-") and not a.startswith("--")]
    assert not any("t" in f or "i" in f for f in flags), call    # no -t / -i


# ======================================================== probe: docker

def test_dm_r6_a_container_that_is_not_running_is_reported_and_is_information_only(
        dproj, fake_docker):
    paths, _ = dproj
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    fake_docker.set(state_="exited")
    r = s.api().probe("fakecli", paths, "docker")
    assert r.state == "container not running"
    assert not set(fake_docker.verbs()) & FORBIDDEN


def test_dm_r6_probe_in_a_running_container_reads_the_version_there(dproj, fake_docker):
    paths, _ = dproj
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    r = s.api().probe("fakecli", paths, "docker")
    assert (r.state, r.version) == ("unverified", "1.2.3")


def test_dm_r3_an_entry_applies_to_the_docker_context_only_with_executor_docker(dproj, fake_docker):
    paths, _ = dproj
    doc = s.runtime_manifest()
    s.write_yaml(s.project_manifest_path(paths), doc)
    d = s.api().probe("fakecli", paths, "docker").digests
    doc["verified"] = [s.verified_entry(d, executor="local")]
    s.write_yaml(s.project_manifest_path(paths), doc)
    assert s.api().probe("fakecli", paths, "docker").state == "unverified"
    doc["verified"] = [s.verified_entry(d, executor="docker")]
    s.write_yaml(s.project_manifest_path(paths), doc)
    assert s.api().probe("fakecli", paths, "docker").state == "verified"
    assert s.api().probe("fakecli", paths, "host").state == "unverified"


def test_dm_r6_docker_probe_timeout_is_a_timeout_state_within_the_deadline(dproj, fake_docker):
    paths, _ = dproj
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    fake_docker.set(exec_="hang")
    start = time.monotonic()
    r = s.api().probe("fakecli", paths, "docker")
    assert r.state == "timeout"
    assert time.monotonic() - start < 10 + 5 + 1


def test_dm_r6_a_docker_probe_failure_is_probe_failed(dproj, fake_docker):
    paths, _ = dproj
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    fake_docker.set(exec_="fail")
    assert s.api().probe("fakecli", paths, "docker").state == "probe_failed"


# ============================================================= doctor

def problems(output: str) -> int:
    last = [l for l in output.strip().splitlines() if l.strip()][-1]
    m = re.match(r"(\d+) problem", last)
    return int(m.group(1)) if m else 0


def without_section(output: str, title: str = "cli dependencies") -> str:
    lines, out, skip = output.splitlines(), [], False
    for line in lines:
        if line == title:
            skip = True
            continue
        if skip and (not line or line[0].isspace()):
            continue
        skip = False
        out.append(line)
    return "\n".join(out)


@pytest.fixture
def dp(tmp_path, monkeypatch, capsys):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    monkeypatch.setenv("PATH", f"{cli.parent}:/usr/bin:/bin")

    def run():
        rc, out = s.doctor(paths.root, capsys)
        return rc, out
    return paths, gdir, run


def test_dm_r6_doctor_prints_a_cli_dependencies_section_after_providers(dp):
    paths, _, run = dp
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = run()
    headers = [l for l in out.splitlines() if l and not l[0].isspace()]
    assert "cli dependencies" in headers
    assert headers.index("providers") < headers.index("cli dependencies")
    assert headers.index("cli dependencies") == headers.index("providers") + 1


def test_dm_r6_doctor_shows_state_version_and_provenance_per_provider(dp):
    paths, _, run = dp
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = run()
    rows = [l for l in s.section(out, "cli dependencies") if "fakecli" in l]
    text = "\n".join(rows)
    assert "unverified" in text and "1.2.3" in text
    assert str(s.project_manifest_path(paths)) in "\n".join(s.section(out, "cli dependencies"))


def test_dm_r6_doctor_prints_both_digests_for_copying(dp):
    paths, _, run = dp
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    d = s.api().probe("fakecli", paths).digests
    _, out = run()
    text = "\n".join(s.section(out, "cli dependencies"))
    assert d["integration_digest"] in text and d["dependencies_digest"] in text


def test_dm_r6_doctor_shows_the_verified_versions_for_the_context(dp):
    paths, _, run = dp
    doc = s.runtime_manifest()
    s.write_yaml(s.project_manifest_path(paths), doc)
    doc["verified"] = [s.verified_entry(s.api().probe("fakecli", paths).digests, version="0.9.0")]
    s.write_yaml(s.project_manifest_path(paths), doc)
    _, out = run()
    assert "0.9.0" in "\n".join(s.section(out, "cli dependencies"))


def test_dm_r6_a_provider_without_a_manifest_says_no_manifest(dp):
    _, _, run = dp
    _, out = run()
    assert "no manifest" in "\n".join(s.section(out, "cli dependencies"))


# ------------------------------------------------------------ exit status

def test_dm_r6_exit_status_unverified_and_no_manifest_add_no_problem(dp, tmp_path):
    paths, _, run = dp
    _, base = run()                                   # no manifest
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = run()                                    # unverified
    assert "unverified" in "\n".join(s.section(out, "cli dependencies"))
    assert problems(out) == problems(base)


def test_dm_r6_exit_status_verified_adds_no_problem(dp):
    paths, _, run = dp
    _, base = run()
    doc = s.runtime_manifest()
    s.write_yaml(s.project_manifest_path(paths), doc)
    doc["verified"] = [s.verified_entry(s.api().probe("fakecli", paths).digests)]
    s.write_yaml(s.project_manifest_path(paths), doc)
    _, out = run()
    assert problems(out) == problems(base)


def test_dm_r6_exit_status_a_malformed_manifest_is_a_problem(dp):
    paths, _, run = dp
    _, base = run()
    s.write_yaml(s.project_manifest_path(paths), "schema: 2\n")
    rc, out = run()
    assert problems(out) == problems(base) + 1
    assert rc == 1
    assert "malformed" in "\n".join(s.section(out, "cli dependencies"))


def test_dm_r6_exit_status_a_disabled_provider_adds_no_problem(tmp_path, monkeypatch, capsys):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli, enabled=False)
    monkeypatch.setenv("PATH", f"{cli.parent}:/usr/bin:/bin")
    _, base = s.doctor(paths.root, capsys)
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = s.doctor(paths.root, capsys)
    assert problems(out) == problems(base)
    assert "disabled" in "\n".join(s.section(out, "cli dependencies"))


def test_dm_r6_exit_status_a_host_missing_binary_is_not_counted_twice(tmp_path, monkeypatch, capsys):
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=None)
    _, base = s.doctor(paths.root, capsys)            # providers section already counts it
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = s.doctor(paths.root, capsys)
    assert problems(out) == problems(base)
    assert "missing" in "\n".join(s.section(out, "cli dependencies"))


def test_dm_r6_a_hung_binary_is_reported_without_becoming_a_problem(tmp_path, monkeypatch, capsys):
    cli = s.write_cli(tmp_path / "bin", body='case "$1" in --version) sleep 60 ;; esac\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    monkeypatch.setenv("PATH", f"{cli.parent}:/usr/bin:/bin")
    _, base = s.doctor(paths.root, capsys)
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    start = time.monotonic()
    _, out = s.doctor(paths.root, capsys)
    assert time.monotonic() - start < 10 + 5 + 30      # the deadline is per probe, not unbounded
    assert "timeout" in "\n".join(s.section(out, "cli dependencies"))
    assert problems(out) == problems(base)


# ------------------------------------------------------- contexts (docker)

def _docker_project(tmp_path, monkeypatch, capsys, *, project_executor, agents):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, gdir = s.make_project(
        tmp_path, monkeypatch, bin_path=cli, agents=agents,
        project_yaml={"executor": {"kind": project_executor}})
    monkeypatch.setenv("PATH", f"{tmp_path / 'dockerbin'}:{cli.parent}:/usr/bin:/bin")
    return paths


AGENT = {"provider": "fakecli", "model": "m", "instructions": "x.md"}


def test_dm_r6_contexts_include_every_per_agent_executor_override(
        tmp_path, monkeypatch, capsys, fake_docker):
    paths = _docker_project(tmp_path, monkeypatch, capsys, project_executor="local",
                            agents={"a1": {**AGENT}, "a2": {**AGENT, "executor": "docker"}})
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    fake_docker.set(state_="exited")
    _, out = s.doctor(paths.root, capsys)
    rows = [l for l in s.section(out, "cli dependencies") if "fakecli" in l]
    assert any("docker" in r and "container not running" in r for r in rows), rows
    assert any("unverified" in r and "docker" not in r for r in rows), rows


def test_dm_r6_contexts_are_not_the_first_match_of_executor_for(
        tmp_path, monkeypatch, capsys, fake_docker):
    paths = _docker_project(
        tmp_path, monkeypatch, capsys, project_executor="docker",
        agents={"a1": {**AGENT, "executor": "local"}, "a2": {**AGENT}})
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    fake_docker.set(state_="exited")
    _, out = s.doctor(paths.root, capsys)
    text = "\n".join(l for l in s.section(out, "cli dependencies") if "fakecli" in l)
    assert "container not running" in text          # the project executor (docker)
    assert "unverified" in text                     # a1's local override


def test_dm_r6_a_container_that_is_not_running_is_information_only_in_doctor(
        tmp_path, monkeypatch, capsys, fake_docker):
    paths = _docker_project(tmp_path, monkeypatch, capsys, project_executor="docker",
                            agents={"a2": {**AGENT}})
    fake_docker.set(state_="exited")
    _, base = s.doctor(paths.root, capsys)
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, out = s.doctor(paths.root, capsys)
    assert "container not running" in "\n".join(s.section(out, "cli dependencies"))
    assert problems(out) == problems(base)
    assert not set(fake_docker.verbs()) & FORBIDDEN


# ================================================================ DM-R7

def test_dm_r7_the_existing_doctor_sections_are_unchanged_by_a_manifest(dp):
    paths, _, run = dp
    _, base = run()
    s.write_yaml(s.project_manifest_path(paths), s.runtime_manifest())
    _, with_manifest = run()
    assert s.section(with_manifest, "cli dependencies")     # the section is there ...
    assert without_section(with_manifest) == without_section(base)


def test_dm_r7_the_existing_doctor_sections_are_unchanged_even_by_a_malformed_manifest(dp):
    paths, _, run = dp
    _, base = run()
    s.write_yaml(s.project_manifest_path(paths), "schema: 2\n")
    _, out = run()
    assert "malformed" in "\n".join(s.section(out, "cli dependencies"))
    strip = lambda t: "\n".join(l for l in without_section(t).splitlines()
                                if not re.match(r"\d+ problem|ok$", l))
    assert strip(out) == strip(base)


def test_dm_r7_launch_routing_and_auth_never_read_the_manifest(tmp_path, monkeypatch):
    """A manifest that cannot even be opened (it is a directory) must not
    disturb provider loading, binary resolution or script lookup."""
    from multiagents.config import load as load_config
    from multiagents.providers import load_providers
    from multiagents.scripts import find_script
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, gdir = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    s.project_manifest_path(paths).mkdir()
    (paths.config / "providers" / "fakecli.sh").write_text("#!/bin/sh\n")
    config = load_config(paths)
    providers = load_providers(config.providers)
    assert providers["fakecli"].usable()
    assert find_script("fakecli.sh", gdir, paths.config) is not None


def test_dm_r7_probe_of_an_unreadable_manifest_is_malformed_not_a_crash(tmp_path, monkeypatch):
    cli = s.write_cli(tmp_path / "bin", body='echo "fakecli 1.2.3"\n')
    paths, _ = s.make_project(tmp_path, monkeypatch, bin_path=cli)
    s.project_manifest_path(paths).mkdir()
    assert s.api().probe("fakecli", paths).state == "malformed"
