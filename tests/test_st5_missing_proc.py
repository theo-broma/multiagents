"""ST5-R2: missing process inspection is unknown, rather than idle."""

import os
import subprocess
import sys

from multiagents import procs
from multiagents.executor import docker


def test_unavailable_proc_tree_is_an_observation_failure(monkeypatch):
    def unavailable(pid):
        raise procs._Gone(pid)

    monkeypatch.setattr(procs, "_stat", unavailable)
    assert procs.has_live_descendants(12345) is None


def test_missing_cli_in_an_observable_tree_has_no_live_children(monkeypatch):
    cli_pid = os.getpid() + 1

    def observable(pid):
        if pid == cli_pid:
            raise procs._Gone(pid)
        return ["S", "1"]

    monkeypatch.setattr(procs, "_stat", observable)
    assert procs.has_live_descendants(cli_pid) is False


def test_container_probe_reports_unavailable_proc_as_unknown():
    source = """
import sys
from pathlib import Path
from unittest.mock import patch
with patch.object(Path, 'read_text', side_effect=FileNotFoundError()):
    exec(sys.argv[2])
"""
    result = subprocess.run([sys.executable, "-c", source, "12345", docker._DESCENDANTS_CHECK],
                            capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == "descendants:unknown"
