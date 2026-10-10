"""PS2-R6: exercise the shipped shell runner with real, tiny pytest runs."""
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
BOUND = 4.0


@pytest.fixture
def miniature(tmp_path):
    root = tmp_path / "mini"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".venv/bin").mkdir(parents=True)
    shutil.copy2(REPO / "scripts/test-chunk.sh", root / "scripts/test-chunk.sh")
    (root / ".venv/bin/python").write_text("#!/bin/sh\nexec " + str(Path(sys.executable)) + ' "$@"\n')
    (root / ".venv/bin/python").chmod(0o755)
    return root


@pytest.fixture
def basetemps():
    owned = []
    def make(prefix="pt-"):
        path = Path(tempfile.mkdtemp(prefix=prefix, dir="/var/tmp"))
        owned.append(path)
        return path
    yield make
    for path in owned:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def run(root, *args):
    return subprocess.run(["bash", str(root / "scripts/test-chunk.sh"), "1", "1",
                           "-q", "-p", "no:cacheprovider", *map(str, args)],
                          capture_output=True, text=True, timeout=BOUND)


@pytest.mark.parametrize("fails", [False, True])
def test_ps2_r6_pass_and_failure_remove_basetemp_and_preserve_status(miniature, basetemps, fails):
    path = basetemps()
    (miniature / "tests/test_sample.py").write_text(f"def test_sample(tmp_path):\n    assert {not fails!r}\n")
    result = run(miniature, "--basetemp=" + str(path))
    assert result.returncode == int(fails), result.stderr
    assert not path.exists(), "pytest exited but its qualifying basetemp remains"


def test_ps2_r6_only_the_effective_last_basetemp_is_removed(miniature, basetemps):
    first, last = basetemps(), basetemps()
    (first / "witness").write_text("keep")
    (miniature / "tests/test_sample.py").write_text("def test_sample(tmp_path): pass\n")
    result = run(miniature, "--basetemp", first, "--basetemp=" + str(last))
    assert result.returncode == 0, result.stderr
    assert (first / "witness").read_text() == "keep"
    assert not last.exists()


@pytest.mark.parametrize("shape", ["ordinary", "direct_wrong_prefix", "nested", "repository_symlink", "safe_symlink"])
def test_ps2_r6_resolves_symlinks_and_keeps_unqualified_paths(miniature, basetemps, tmp_path, shape):
    if shape == "ordinary":
        path = tmp_path / "pytest"
        target = path
    elif shape == "direct_wrong_prefix":
        path = basetemps("pytest-")
        target = path
    elif shape == "nested":
        path = basetemps() / "pt-nested"
        target = path
    elif shape == "repository_symlink":
        repo = basetemps()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        target = repo / "pt-inside"
        path = basetemps()
        path.rmdir()
        path.symlink_to(target, target_is_directory=True)
    else:
        target = basetemps()
        path = basetemps()
        path.rmdir()
        path.symlink_to(target, target_is_directory=True)
    target.mkdir(exist_ok=True)
    (target / "witness").write_text("keep until script cleanup")
    (miniature / "tests/test_sample.py").write_text("def test_sample(): pass\n")
    result = run(miniature, "--basetemp=" + str(path))
    assert result.returncode == 0, result.stderr
    if shape == "safe_symlink":
        assert not target.exists()
    else:
        assert target.is_dir()
        notices = [line for line in (result.stdout + result.stderr).splitlines()
                   if line.strip() and line != "tests/test_sample.py"
                   and "[100%]" not in line
                   and not re.fullmatch(r"1 passed in .*", line)]
        assert len(notices) == 1, "kept basetemp must produce one notice line"


def test_ps2_r6_interrupt_cleans_basetemp_and_preserves_signal_status(miniature, basetemps, tmp_path):
    path = basetemps()
    ready = tmp_path / "ready"
    (miniature / "tests/test_sample.py").write_text(
        "from pathlib import Path\nimport signal\n"
        f"def test_sample(tmp_path):\n    Path({str(ready)!r}).touch()\n    signal.pause()\n")
    proc = subprocess.Popen(["bash", str(miniature / "scripts/test-chunk.sh"), "1", "1",
                             "-q", "--basetemp=" + str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        end = time.monotonic() + BOUND
        while not ready.exists() and proc.poll() is None and time.monotonic() < end:
            time.sleep(0.02)
        assert ready.exists(), "tiny pytest did not reach its signal wait"
        os.killpg(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=BOUND)
        assert proc.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM)
        assert not path.exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=BOUND)


def test_ps2_r6_documentation_delegates_cleanup_to_the_runner():
    doc = (REPO / "AGENTS.md").read_text()
    assert "rm -rf" not in doc
    assert "/var/tmp" in doc and "pt-" in doc
