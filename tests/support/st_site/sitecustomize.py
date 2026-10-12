"""Carry ST's agreed filesystem seam into its independent Python processes.

The daemon double owns temporary mount sources whose filesystem is simulated
as unknown. This makes test-container policy deterministic with a disk-backed
pytest basetemp; ST1-R1 overrides the reading to test persistent filesystems.
Docker shim/daemon interpreters need no application import.
"""
import os
import sys
from pathlib import Path

if os.environ.get('ST_DOCKER_DIR') and Path(sys.argv[0]).name != 'docker':
    import pytest
    from multiagents.executor import docker
    patch = pytest.MonkeyPatch()
    patch.setattr(docker, 'filesystem_type', lambda path: None, raising=False)
