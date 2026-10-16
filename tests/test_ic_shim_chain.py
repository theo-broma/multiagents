"""The label shim and the session guard chain in either order on PATH without
looping, reach the final `docker` exactly once, and both keep their effects."""
import json
import os
import subprocess
import sys

import pytest

from tests.support.ic_docker import OWNER_LABEL, write_label_shim
from tests.support.st_container_guard import DockerGuard

BOUND = 10


def _build(tmp_path):
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    out = tmp_path / "reached.jsonl"
    fake = fake_dir / "docker"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                    f"open({str(out)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    fake.chmod(0o755)
    label_dir = write_label_shim(tmp_path / "label", "owner-x")
    guard = DockerGuard(tmp_path / "guard", "owner-x")
    return fake_dir, label_dir, guard, out


@pytest.mark.parametrize("label_first", [True, False])
@pytest.mark.parametrize("argv", [["network", "create", "n1"], ["volume", "create", "v1"]])
def test_shims_chain_terminates_and_reaches_docker_once(tmp_path, label_first, argv):
    fake_dir, label_dir, guard, out = _build(tmp_path)
    order = [label_dir, guard.directory] if label_first else [guard.directory, label_dir]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*map(str, order), str(fake_dir), "/usr/bin", "/bin"])
    env.pop("ST_TEST_DOCKER_LIVE", None)
    first = str((order[0]) / "docker")
    done = subprocess.run([first, *argv], env=env, timeout=BOUND, capture_output=True)
    assert done.returncode == 0, done.stderr
    reached = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(reached) == 1
    assert f"{OWNER_LABEL}=owner-x" in reached[0]
    assert reached[0][:2] == argv[:2]
    records = guard.records()
    assert len(records) == 1 and records[0]["argv"][:2] == argv[:2]
