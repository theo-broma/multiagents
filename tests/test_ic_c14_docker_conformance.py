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

from test_ic_c5_conformance import finished, out, provisioned, started  # noqa: E402

pytestmark = pytest.mark.skipif(not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1")


def image_python(code: str) -> list[str]:
    return ["python3", "-c", code]


@pytest.fixture(params=["docker"])
def be(request, tmp_path, monkeypatch, st_live_docker):
    dk.require_docker()                                          # a failure, never a skip, under the switch
    owner = dk.test_owner(request)
    shim = dk.write_label_shim(tmp_path / "ic-label-shim", owner)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
    dk.reap_once()
    dk.register_session_finalizer(owner)
    monkeypatch.setattr(conformance, "py", image_python)
    # the suite builds a "restarted host" backend with the reference factory: here, a fresh Docker one
    monkeypatch.setattr(conformance.ic_backends, "make_reference",
                        lambda root, seeds=None: dk.keyed(s3.docker_backend(root), owner))
    root = tmp_path / "backend-root"
    root.mkdir()
    backend = dk.keyed(s3.docker_backend(root), owner)
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


# ----------------------------------------------- Docker-specific verdicts
#
# These names shadow the star-imported reference tests of the same name: on
# Docker the contract differs (D-S3.2 / IC-C14, IC-C5 K9), so the case is
# restated, not skipped. The reference backend still runs the originals.

def _lifetime_unsupported(be, owner):
    backend, root, _ = be
    got = run(backend.provision(ic.launch_spec(root, owner=owner, lifetime="conversation"), "key-1"))
    assert ic.kind_of(got) == "unsupported" and ic.field(got, "field") == "lifetime", got


def test_ic_c4_a_conversation_resource_carries_one_invocation_per_turn(be):
    _lifetime_unsupported(be, "conv_1")                  # D-S3.2: conversation lifetime waits for slice 6


def test_ic_c4_a_user_shell_is_an_invocation_of_its_own_kind(be):
    _lifetime_unsupported(be, "conv_2")


def test_ic_c5_k2_same_key_different_invocation_spec_is_an_identity_conflict_and_starts_nothing(be):
    _lifetime_unsupported(be, "conv_k2")


def test_ic_c5_k9_each_invocation_gets_a_private_home_and_tmpdir(be):
    """Guest paths are fixed by the contract (/work/home, /work/tmp), so they
    are equal across containers; privacy is the mount behind them. A file one
    resource's invocation writes to HOME or TMPDIR is invisible to another's."""
    probe_write = ("import os, pathlib\n"
                   "for var in ('HOME', 'TMPDIR'):\n"
                   "    pathlib.Path(os.environ[var], 'only-in-a').write_text('a')\n"
                   "print('written', flush=True)")
    probe_look = ("import os, pathlib\n"
                  "print(sorted(var for var in ('HOME', 'TMPDIR')"
                  " if pathlib.Path(os.environ[var], 'only-in-a').exists()), flush=True)")
    _, a = provisioned(be)
    started(be, probe_write, key="inv-1", ident=a)
    finished(be, a, "inv-1", conformance.DOCKER_OP)
    assert out(be, a, "inv-1") == b"written\n"
    started(be, probe_look, key="inv-2", ident=a)                     # sanity: visible where written
    finished(be, a, "inv-2", conformance.DOCKER_OP)
    assert out(be, a, "inv-2") == b"['HOME', 'TMPDIR']\n"
    _, b = provisioned(be, "key-2", owner="att_other")
    started(be, probe_look, key="inv-1", ident=b)
    finished(be, b, "inv-1", conformance.DOCKER_OP)
    assert out(be, b, "inv-1") == b"[]\n"
