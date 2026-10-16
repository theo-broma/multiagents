"""Self-test of the fake `docker` CLI (tests/support/fake_docker.py). These are
the only green tests of slice 3: they pin the fake itself, so a failure in an
IC-C* test cannot be a fault of the double."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from fake_docker import FakeDocker, may_lose_log_data, memory_bytes, mounts_of  # noqa: E402


@pytest.fixture
def fd(tmp_path):
    return FakeDocker(tmp_path)


def docker(fd, *args, check=False):
    return subprocess.run([str(fd.docker), *args], capture_output=True, text=True, timeout=20, check=check)


def create(fd, *args):
    return docker(fd, "create", *args, "img:1", "cmd", check=True).stdout.strip()


def test_create_parses_options_into_the_inspect_shape(fd, tmp_path):
    src = tmp_path / "c"
    src.mkdir()
    cid = create(fd, "--name", "n1", "--label", "a=b", "-l", "c=d", "--user", "1000:1000", "--network", "none",
                 "--memory=64m", "--pids-limit", "99", "--cpu-shares=512", "--cpus", "1.5",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--restart", "no",
                 "-v", "/h/a:/g/a", "-v", "/h/b:/g/b:ro", "--mount", f"type=bind,src={src},dst=/g/c,readonly",
                 "--log-driver", "local", "--log-opt", "max-size=-1", "-e", "X=1")
    c = fd.container(cid)
    assert len(cid) == 64 and c["Name"] == "/n1" and c["Config"]["Labels"] == {"a": "b", "c": "d"}
    h = c["HostConfig"]
    assert (c["Config"]["User"], h["NetworkMode"], h["Memory"], h["PidsLimit"], h["CpuShares"], h["NanoCpus"]) == \
        ("1000:1000", "none", 64 * 2 ** 20, 99, 512, 1_500_000_000)
    assert h["CapDrop"] == ["ALL"] and h["SecurityOpt"] == ["no-new-privileges"] and h["Privileged"] is False
    assert sorted((m["source"], m["destination"], m["rw"]) for m in mounts_of(c)) == [
        ("/h/a", "/g/a", True), ("/h/b", "/g/b", False), (str(src), "/g/c", False)]
    assert [h["exists"] for h in c["HostSources"]] == [True, False, False]   # -v sources, then --mount
    assert not may_lose_log_data(c) and c["Config"]["Cmd"] == ["cmd"] and c["Config"]["Image"] == "img:1"


def test_a_name_in_use_and_a_missing_image_are_dockers_errors(fd):
    create(fd, "--name", "n1")
    again = docker(fd, "create", "--name", "n1", "img:1")
    assert again.returncode == 125 and "already in use" in again.stderr
    fd.set_images(["only:1"])
    missing = docker(fd, "create", "img:1")
    assert missing.returncode == 125 and "Unable to find image" in missing.stderr
    assert docker(fd, "image", "inspect", "only:1").returncode == 0
    assert docker(fd, "image", "inspect", "img:1").returncode == 1


def test_lifecycle_start_stop_rm_and_output(fd):
    cid = create(fd, "--name", "n1")
    fd.workload(exit_code=3, stdout=b"out1\nout2\n", stderr=b"err\n")
    assert docker(fd, "start", cid).returncode == 0
    state = json.loads(docker(fd, "inspect", cid).stdout)[0]["State"]
    assert (state["Status"], state["ExitCode"], state["OOMKilled"]) == ("exited", 3, False)
    assert state["FinishedAt"] != "0001-01-01T00:00:00Z"
    logs = docker(fd, "logs", cid)
    assert (logs.stdout, logs.stderr) == ("out1\nout2\n", "err\n")
    assert docker(fd, "logs", "--tail", "1", cid).stdout == "out2\n"
    assert docker(fd, "inspect", "--format", "{{.State.ExitCode}}", cid).stdout.strip() == "3"
    assert docker(fd, "inspect", "--format", "{{json .State.OOMKilled}}", cid).stdout.strip() == "false"
    docker(fd, "start", cid)                              # a start of an exited one runs it again
    assert fd.start_count(cid) == 2
    assert docker(fd, "rm", cid).returncode == 0
    gone = docker(fd, "inspect", cid)
    assert gone.returncode == 1 and "No such object" in gone.stderr


def test_a_running_container_needs_stop_or_force_and_stop_is_idempotent(fd):
    cid = create(fd)
    fd.workload(runs=True)
    docker(fd, "start", cid)
    assert docker(fd, "rm", cid).returncode == 1
    assert docker(fd, "stop", "-t", "1", cid).returncode == 0
    assert fd.container(cid)["State"]["ExitCode"] == 143
    assert docker(fd, "stop", cid).returncode == 0
    assert docker(fd, "rm", cid).returncode == 0


def test_ps_filters_by_label_and_hides_nothing_with_a(fd):
    a = create(fd, "--label", "p=1", "--label", "k=x")
    b = create(fd, "--label", "p=2")
    ids = docker(fd, "ps", "-aq", "--no-trunc", "--filter", "label=p=1").stdout.split()
    assert ids == [a]
    assert docker(fd, "ps", "-aq", "--no-trunc", "--filter", "label=p").stdout.split() == [a, b]
    assert docker(fd, "ps", "-q").stdout.split() == []


def test_rules_and_the_call_log(fd):
    fd.rule("inspect", effect="daemon_down", times=1)
    first = docker(fd, "inspect", "x")
    assert first.returncode == 1 and "Cannot connect to the Docker daemon" in first.stderr
    assert docker(fd, "inspect", "x").stderr.startswith("Error: No such object")
    fd.rule("create", effect="after_effect", times=1)
    out = docker(fd, "create", "--name", "z", "img:1")
    assert out.returncode == 1 and fd.container("z") is not None   # it happened; the CLI reported failure
    assert docker(fd, "exec", "x", "ls").returncode == 125 and fd.unsupported() == [["exec", "x", "ls"]]
    assert fd.calls_of("inspect") and fd.calls()[0] == ["inspect", "x"]


def test_global_options_and_the_container_prefix_are_folded(fd):
    docker(fd, "--host", "unix:///x", "container", "ls", "-a")
    assert fd.commands() == [["ls", "-a"]]


def test_memory_and_log_helpers():
    assert memory_bytes("6g") == 6 * 2 ** 30 and memory_bytes("512") == 512
    fake = lambda t, **cfg: {"HostConfig": {"LogConfig": {"Type": t, "Config": cfg}}}  # noqa: E731
    assert may_lose_log_data(fake("local")) and not may_lose_log_data(fake("local", **{"max-size": "-1"}))
    assert may_lose_log_data(fake("json-file", **{"max-size": "10m"})) and not may_lose_log_data(fake("json-file"))
    assert may_lose_log_data(fake("syslog")) and may_lose_log_data(fake("none"))


@pytest.mark.parametrize("text,expected", [
    ("1t", 2 ** 40), (".5g", 2 ** 29), ("512mb", 512 * 2 ** 20), ("6GB", 6 * 2 ** 30),
    ("64M", 64 * 2 ** 20), ("1k", 1024), ("100b", 100), ("2KB", 2048), ("1.5g", 3 * 2 ** 29),
])
def test_memory_formats_docker_accepts_are_accepted(fd, text, expected):
    assert memory_bytes(text) == expected
    cid = create(fd, "--memory", text)
    assert fd.container(cid)["HostConfig"]["Memory"] == expected


@pytest.mark.parametrize("text", ["", "g", "1x", "1gg", "one", "1.2.3m"])
def test_memory_formats_docker_rejects_are_rejected(fd, text):
    out = docker(fd, "create", "--memory", text, "img:1")
    assert out.returncode == 125 and "invalid size" in out.stderr


def test_a_negative_max_size_fails_the_start_and_the_log_read_as_docker_does(fd):
    cid = create(fd, "--log-driver", "local", "--log-opt", "max-size=-1")
    started = docker(fd, "start", cid)
    assert started.returncode != 0
    assert "failed to initialize logging driver: invalid size: '-1'" in started.stderr
    assert json.loads(docker(fd, "inspect", cid).stdout)[0]["State"]["Status"] == "created"
    read = docker(fd, "logs", cid)
    assert read.returncode != 0 and "invalid size: '-1'" in read.stderr


def test_a_valid_max_size_starts_and_reads_normally(fd):
    fd.workload(exit_code=0, stdout=b"hi\n")
    cid = create(fd, "--log-driver", "json-file", "--log-opt", "max-size=10m")
    assert docker(fd, "start", cid).returncode == 0
    logs = docker(fd, "logs", cid)
    assert logs.returncode == 0 and logs.stdout == "hi\n"
