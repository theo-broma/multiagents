"""Adversary: SE3 (F63) — credential_drift with paths the shell really sees.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE3.

`tests/test_se_credential_drift_argv.py` maps every host path to a plain
container view (`views/vN`) before running the exec'd command, so the shell
never stats a hostile name, and its failing exec prints nothing. Three
mutations of `credential_drift` survive that file: dropping `--`, unquoting
`"$path"`, and ignoring a nonzero return. This fake keeps the paths as they
are: `exec` runs the command on this host unchanged, with the paths exactly as
given, and only afterwards replaces each file by rename, so the host sees a new
inode and the container's answer is the old one — drift on every path.

A fake `docker` first on PATH; no daemon, no network. Fuzz seed: SEED below.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths
from multiagents.providers import Provider

SEED = 20261013

FAKE_DOCKER = r"""#!{python}
import json, os, subprocess, sys
state = os.environ["SE_ADV_DOCKER_DIR"]
argv = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps([os.fsencode(a).hex() for a in argv]) + "\n")
config = json.load(open(os.path.join(state, "config.json")))
if argv[:1] == ["inspect"]:
    print("running")
    sys.exit(0)
if argv[:1] != ["exec"]:
    sys.exit(1)
command = argv[2:]
done = subprocess.run(command, capture_output=True, cwd=config.get("cwd", os.path.join(state, "cwd")))
sys.stdout.buffer.write(done.stdout)
# The host renames a fresh file over each path the exec was given.
for element in command:
    if os.path.isfile(element) and not os.path.islink(element):
        fresh = element + ".fresh"
        with open(fresh, "w") as handle:
            handle.write("{}")
        os.replace(fresh, element)
sys.exit(config.get("exit", done.returncode))
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "docker"
    (state / "cwd").mkdir(parents=True)
    (state / "config.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER.replace("{python}", sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SE_ADV_DOCKER_DIR", str(state))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path, state


def executor_for(tmp_path: Path, home: str, links: list[str]):
    paths = []
    for relative in links:
        path = Path(home) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        paths.append(path)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    provider = Provider.from_dict("claude", {"bin": "claude", "home_links": links})
    executor = DockerExecutor({"image": "i"}, ProjectPaths(project), {"claude": provider}, project)
    return executor, paths


def inodes(paths):
    return [str(os.stat(p).st_ino) for p in paths]


def execs(state: Path) -> list[list[bytes]]:
    log = state / "calls.jsonl"
    calls = [[bytes.fromhex(a) for a in json.loads(line)] for line in log.read_text().splitlines()]
    return [c for c in calls if c[:1] == [b"exec"]]


def expect_drift(paths, old):
    return [{"path": str(p), "host_inode": str(p.stat().st_ino), "container_inode": o}
            for p, o in zip(paths, old)]


# ---------------------------------------------------------------------------
# Hostile names, stat'ed by a real shell
# ---------------------------------------------------------------------------

HOSTILE = [
    "with space/cred.json",
    "two  spaces/cred.json",
    "glob*/cred.json",
    "$(touch PWNED)/cred.json",
    "`touch PWNED2`/cred.json",
    "quote'single/cred.json",
    'quote"double/cred.json',
    "new\nline/cred.json",
    "tab\there/cred.json",
    "$HOME/cred.json",
    "back\\slash/cred.json",
]


def test_se3_hostile_names_are_stated_as_themselves_in_path_order(world, monkeypatch):
    tmp_path, state = world
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    executor, paths = executor_for(tmp_path, str(tmp_path / "home"), HOSTILE)
    old = inodes(paths)
    assert executor.credential_drift() == expect_drift(paths, old)
    assert os.listdir(state / "cwd") == [], "a shell parsed a path"


def test_se3_a_path_starting_with_a_dash_is_not_read_as_a_stat_option(world, monkeypatch):
    """HOME is a relative directory named `-L`: every candidate path begins
    with `-L/`, which GNU stat would otherwise take for `-L` (follow links)."""
    tmp_path, state = world
    monkeypatch.setenv("HOME", "-L")
    executor, paths = executor_for(tmp_path, "-L", [".claude/.credentials.json", ".b/two.json"])
    # The container's working directory is the one the relative HOME means.
    (state / "config.json").write_text(json.dumps({"cwd": str(tmp_path)}))
    old = inodes(paths)
    assert executor.credential_drift() == expect_drift(paths, old)


def test_se3_each_path_is_its_own_argv_element_byte_for_byte(world, monkeypatch):
    tmp_path, state = world
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    names = ["latin1-\udce9/cred.json", "x" * 200 + "/" + "y" * 200 + "/cred.json",
             "-n/cred.json", ";rm -rf ~;/cred.json"]
    executor, paths = executor_for(tmp_path, home, names)
    executor.credential_drift()
    (argv,) = execs(state)
    wanted = [os.fsencode(p) for p in paths]
    for path in wanted:
        assert argv.count(path) == 1
        assert all(element == path or path not in element for element in argv)
    # In path order, and as the trailing arguments.
    assert argv[-len(wanted):] == wanted


def test_se3_a_nul_in_a_home_link_neither_raises_nor_drops_the_other_paths(world, monkeypatch):
    tmp_path, state = world
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    executor, paths = executor_for(tmp_path, home, [".a/one.json"])
    executor.providers["claude"].home_links.insert(0, "bad\x00name/cred.json")
    old = inodes(paths)
    assert executor.credential_drift() == expect_drift(paths, old)


# ---------------------------------------------------------------------------
# Failure: a nonzero exec that printed part of its answer
# ---------------------------------------------------------------------------

def test_se3_a_nonzero_exec_discards_inodes_it_had_already_printed(world, monkeypatch):
    """SE3: a nonzero return from any `docker exec` makes the whole result
    `[]`, discarding paths already read. Here the exec printed all three
    answers and then returned 137 (the container was killed on the way out)."""
    tmp_path, state = world
    home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", home)
    executor, paths = executor_for(tmp_path, home, [".a/one.json", ".b/two.json", ".c/three.json"])
    (state / "config.json").write_text(json.dumps({"exit": 137}))
    assert executor.credential_drift() == []


# ---------------------------------------------------------------------------
# Seeded fuzz over names
# ---------------------------------------------------------------------------

NAME_CHARS = [chr(c) for c in range(1, 128) if chr(c) != "/"] + [
    "é", "​", "\U0001f600", "\udcff", "\udc80", "é"]


def test_se3_fuzzed_names_report_every_drift_in_order(world, monkeypatch):
    tmp_path, state = world
    rng = random.Random(SEED)
    bad = []
    for round_ in range(25):
        home = str(tmp_path / f"home{round_}")
        monkeypatch.setenv("HOME", home)
        names = []
        while len(names) < rng.randint(1, 5):
            name = "".join(rng.choice(NAME_CHARS) for _ in range(rng.randint(1, 12)))
            if name in (".", "..") or f"d{len(names)}" in name:
                continue
            names.append(f"d{len(names)}{name}/cred.json")
        executor, paths = executor_for(tmp_path, home, names)
        old = inodes(paths)
        got = executor.credential_drift()
        if got != expect_drift(paths, old):
            bad.append((names, got))
    assert not bad, f"seed {SEED}: {bad[:2]}"
    assert os.listdir(state / "cwd") == []
