"""ST1-R4: a test-provided Docker double or explicit opt-in, never ambient Docker."""
import os

import pytest


@pytest.fixture(scope="session")
def st_unavailable_docker(tmp_path_factory):
    """Fallback daemon double; test-provided doubles precede it on PATH."""
    directory = tmp_path_factory.mktemp("st-unavailable-docker")
    binary = directory / "docker"
    binary.write_text("#!/bin/sh\necho 'real Docker requires SG_TEST_DOCKER=1 and st_live_docker' >&2\nexit 127\n")
    binary.chmod(0o755)
    return directory


@pytest.fixture(autouse=True)
def st_docker_daemon_is_opt_in(request, monkeypatch, st_docker_guard,
                             st_unavailable_docker):
    """Keep incidental CLI probes off the real daemon, including child CLIs.

    Both SG_TEST_DOCKER=1 and the live fixture are required. The R5 recording
    guard remains first on PATH, so unsafe runs still fail the suite.
    """
    if (os.environ.get("SG_TEST_DOCKER") == "1"
            and "st_live_docker" in request.fixturenames):
        return
    monkeypatch.setenv("ST_TEST_DOCKER_FALLBACK", str(st_unavailable_docker))
    monkeypatch.setenv("PATH", os.pathsep.join((str(st_docker_guard.directory),
                       str(st_unavailable_docker), os.environ.get("PATH", ""))))
