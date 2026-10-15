"""IC-C14 — the IC-C5 conformance suite against the REAL Docker backend, the
`ic_milestone(3)` containment cases included (escaped descendants, sibling
reads). Opt-in: SG_TEST_DOCKER=1; with the switch a missing prerequisite is a
failure (IC-C6). Not runnable in the authoring sandbox (no daemon): collected
and read only.

It re-collects every test of `test_ic_c5_conformance` with a `be` fixture that
builds the Docker backend (`ic_s3.docker_backend`), labels everything it
creates per K12 and removes it in teardown, even on failure. `py()` runs the
image's `python3` (the host interpreter's path does not exist in the image).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import identity_of, run  # noqa: E402

import test_ic_c5_conformance as conformance  # noqa: E402
from test_ic_c5_conformance import *  # noqa: E402,F401,F403  (the tests themselves)

pytestmark = pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")


def image_python(code: str) -> list[str]:
    return ["python3", "-c", code]


@pytest.fixture(params=["docker"])
def be(request, tmp_path, monkeypatch, st_live_docker):
    dk.require_docker()                                          # a failure, never a skip, under the switch
    owner = request.config.st_docker_owner
    shim = dk.write_label_shim(tmp_path / "ic-label-shim", owner)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
    dk.reap_once()
    dk.register_session_finalizer(owner)
    monkeypatch.setattr(conformance, "py", image_python)
    # the suite builds a "restarted host" backend with the reference factory: here, a fresh Docker one
    monkeypatch.setattr(conformance.ic_backends, "make_reference",
                        lambda root, seeds=None: s3.docker_backend(root))
    root = tmp_path / "backend-root"
    root.mkdir()
    backend = s3.docker_backend(root)
    yield backend, root, "docker"

    async def cleanup():
        for ident in await backend.list_owned("ic-test"):
            try:
                await backend.stop(identity_of(ident), 0.1)
                await backend.remove(identity_of(ident), False)
            except Exception:
                pass
    try:
        run(cleanup(), timeout=60)
    except Exception:
        pass
    dk.remove_session_resources(owner)
