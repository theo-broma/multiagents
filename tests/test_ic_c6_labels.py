"""IC-C6 / K12 — the test-session label on everything a test creates, and
reaping that touches only dead sessions' resources.

These need no Docker daemon: a stub `docker` records what it is asked and
answers listings and inspections from a script. They run always (not behind
SG_TEST_DOCKER), because a labelling gap is a leak whether or not the real
suite is switched on.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
from st_container_guard import process_start  # noqa: E402

OWNER = "host:1:2"
LABEL = f"{dk.OWNER_LABEL}={OWNER}"

STUB = '''#!{python}
import json, os, sys
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with (state / "argv.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")
script = json.loads((state / "script.json").read_text())
key = " ".join(args[:3])
for pattern, reply in script.items():
    if key.startswith(pattern):
        print(reply)
        break
'''


@pytest.fixture
def stub(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "script.json").write_text("{}")
    (state / "argv.jsonl").touch()
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(STUB.format(python=sys.executable, state=str(state)))
    exe.chmod(0o755)
    shim = dk.write_label_shim(tmp_path / "shim", OWNER)

    class S:
        env = dict(os.environ, PATH=f"{shim}{os.pathsep}{bindir}{os.pathsep}{os.environ['PATH']}")
        bin = str(bindir / "docker")

        @staticmethod
        def script(**replies):
            (state / "script.json").write_text(json.dumps(replies))

        @staticmethod
        def calls():
            return [json.loads(x) for x in (state / "argv.jsonl").read_text().splitlines()]

        @staticmethod
        def call(*args):
            return subprocess.run([str(shim / "docker"), *args], env=S.env, capture_output=True,
                                  text=True, timeout=10)
    return S


@pytest.mark.parametrize("command", [["run", "-d", "img"], ["create", "--name", "x", "img"],
                                     ["container", "create", "img"], ["container", "run", "img"],
                                     ["network", "create", "net"], ["volume", "create", "vol"]])
def test_ic_c6_k12_every_resource_creating_command_carries_the_session_label(stub, command):
    stub.call(*command)
    (seen,) = stub.calls()
    assert "--label" in seen and seen[seen.index("--label") + 1] == LABEL
    # the label is an option of the subcommand: it comes before the image / name operand
    assert seen.index("--label") < len(seen) - 1 and seen[-1] == command[-1]


@pytest.mark.parametrize("command", [["ps", "-aq"], ["rm", "-f", "x"], ["inspect", "x"],
                                     ["exec", "x", "ls"], ["network", "ls"], ["volume", "rm", "v"],
                                     ["image", "inspect", "img"]])
def test_ic_c6_k12_other_commands_pass_through_untouched(stub, command):
    stub.call(*command)
    assert stub.calls() == [command]


def test_ic_c6_k12_a_label_already_present_is_not_doubled(stub):
    stub.call("create", "--label", LABEL, "img")
    assert stub.calls()[0].count(LABEL) == 1


def alive_label():
    return f"{socket.gethostname()}:{os.getpid()}:{process_start(os.getpid())}"


def dead_label():
    return f"{socket.gethostname()}:999999:1"


def inspect_reply(kind, entries):
    key = "Config" if kind == "container" else None
    out = []
    for ident, label in entries:
        labels = {dk.OWNER_LABEL: label} if label is not None else {}
        item = {"Id": ident, "Name": ident}
        item.update({"Config": {"Labels": labels}} if key else {"Labels": labels})
        out.append(item)
    return json.dumps(out)


@pytest.mark.parametrize("kind,ls", [("container", "ps -aq"), ("network", "network ls"),
                                     ("volume", "volume ls")])
def test_ic_c6_k12_reaping_removes_only_dead_sessions_resources(stub, kind, ls):
    entries = [("dead1", dead_label()), ("live1", alive_label()), ("other-host", f"elsewhere:{os.getpid()}:1"),
               ("garbled", "not-a-label"), ("unlabelled", None)]
    stub.script(**{ls: "\n".join(i for i, _ in entries), f"{kind} inspect": inspect_reply(kind, entries)})
    removed = dk.reap_dead_sessions(stub.bin)
    assert removed == [f"{kind}:dead1"]
    rms = [c for c in stub.calls() if "rm" in c]
    assert [c[-1] for c in rms] == ["dead1"]


def test_ic_c6_k12_a_reused_pid_with_another_start_time_counts_as_dead(stub):
    reused = f"{socket.gethostname()}:{os.getpid()}:{int(process_start(os.getpid())) + 1}"
    stub.script(**{"ps -aq": "c1", "container inspect": inspect_reply("container", [("c1", reused)])})
    assert dk.reap_dead_sessions(stub.bin) == ["container:c1"]


def test_ic_c6_k12_an_unavailable_daemon_gives_no_authority_to_remove_anything(stub):
    stub.script(**{"ps -aq": "c1"})                     # inspect answers nothing parseable
    assert dk.reap_dead_sessions(stub.bin) == []
    assert not [c for c in stub.calls() if "rm" in c]


# ---------------------------------------------------------------- teardown

@pytest.mark.parametrize("kind,ls", [("container", "ps -aq"), ("network", "network ls"),
                                     ("volume", "volume ls")])
def test_ic_c6_teardown_removes_this_sessions_resource_and_no_one_elses(stub, kind, ls):
    entries = [("mine", OWNER), ("live-other", alive_label()), ("dead-other", dead_label()),
               ("unlabelled", None)]
    stub.script(**{ls: "\n".join(i for i, _ in entries), f"{kind} inspect": inspect_reply(kind, entries)})
    removed = dk.remove_session_resources(OWNER, stub.bin)
    assert removed == [f"{kind}:mine"]
    assert [c[-1] for c in stub.calls() if "rm" in c] == ["mine"]


def test_ic_c6_teardown_removes_container_network_and_volume_together(stub):
    stub.script(**{"ps -aq": "c1", "container inspect": inspect_reply("container", [("c1", OWNER)]),
                   "network ls": "n1", "network inspect": inspect_reply("network", [("n1", OWNER)]),
                   "volume ls": "v1", "volume inspect": inspect_reply("volume", [("v1", OWNER)])})
    assert sorted(dk.remove_session_resources(OWNER, stub.bin)) == \
        ["container:c1", "network:n1", "volume:v1"]


def test_ic_c6_teardown_with_an_unavailable_daemon_removes_nothing(stub):
    stub.script(**{"ps -aq": "c1"})
    assert dk.remove_session_resources(OWNER, stub.bin) == []
    assert not [c for c in stub.calls() if "rm" in c]


def test_ic_c6_start_reaping_still_spares_the_current_session(stub):
    entries = [("mine", alive_label()), ("dead1", dead_label())]
    stub.script(**{"ps -aq": "mine\ndead1", "container inspect": inspect_reply("container", entries)})
    assert dk.reap_dead_sessions(stub.bin) == ["container:dead1"]


def test_ic_c6_teardown_runs_after_a_failing_test(stub, tmp_path):
    """A fixture shaped like the isolation world's, under a real pytest run
    whose test fails: its teardown still removes this session's container."""
    stub.script(**{"ps -aq": "mine", "container inspect": inspect_reply("container", [("mine", OWNER)])})
    test = tmp_path / "test_inner.py"
    test.write_text(
        "import sys, pytest\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent / 'support')!r})\n"
        "import ic_docker as dk\n"
        "class W:\n"
        f"    owner = {OWNER!r}; tmp_marker = 'nowhere-ic-c6'\n"
        "    def close(self): pass\n"
        "@pytest.fixture\n"
        "def world():\n"
        "    yield W()\n"
        "    dk.cleanup(W())\n"
        "def test_fails(world):\n"
        "    assert False\n")
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "-p", "no:xdist", "--rootdir", str(tmp_path), str(test)],
                         env=dict(stub.env, PYTHONPATH=os.environ.get("PYTHONPATH", "")),
                         capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert "1 failed" in run.stdout, run.stdout
    assert [c[-1] for c in stub.calls() if c[:2] == ["container", "rm"]] == ["mine"]
