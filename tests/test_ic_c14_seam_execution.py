"""K17.3: execute against attached checkout/HOME and snapshotted stdin.

The fake CLI cannot demonstrate guest content, so Docker uses the opt-in daemon.
"""
from pathlib import Path
import io
import os
import sys
import time
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_backends
import ic_docker as dk
import ic_s3 as s3
import ic_support as ic

WAIT = 4


@pytest.fixture(params=["reference", pytest.param("docker", marks=pytest.mark.skipif(
    not dk.OPT_IN, reason="real Docker is opt-in: SG_TEST_DOCKER=1"))])
def executable_backend(request, tmp_path, monkeypatch, st_live_docker):
    root = tmp_path / "root"
    root.mkdir()
    owner = None
    if request.param == "docker":
        dk.require_docker()
        owner = request.config.st_docker_owner
        shim = dk.write_label_shim(tmp_path / "shim", owner)
        monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
        backend = s3.docker_backend(root)
    else:
        backend = ic_backends.make_reference(root)
    assert callable(getattr(backend, "attach_checkout", None)), "K17.3 attach_checkout is missing"
    identities = []
    yield backend, root, request.param, identities
    for identity in identities:
        ic.run(backend.stop(identity, 0.1))
        ic.run(backend.remove(identity, False))
    if owner:
        dk.remove_session_resources(owner)


def test_k17_3_execution_sees_attached_guest_content_home_and_exact_stdin(executable_backend):
    backend, root, name, identities = executable_backend
    identity = ic.identity_of(ic.run(backend.provision(ic.launch_spec(root), "execution-seam")))
    identities.append(identity)
    checkout, home = root / "attached", root / "home"
    checkout.mkdir()
    home.mkdir()
    (checkout / "repo-marker").write_bytes(b"checkout\n")
    (home / "home-marker").write_bytes(b"home\n")
    assert ic.identity_of(ic.run(backend.attach_checkout(identity, checkout, home))) == identity
    assert ic.identity_of(ic.run(backend.attach_checkout(identity, checkout))) == identity  # HOME preserved
    framed = b"prompt\x00\xff\n"
    source = io.BytesIO(framed)
    snapshot = ic.run(backend.prepare_input(identity, source))
    source.seek(0)
    source.write(b"different")
    code = ("import os,sys;from pathlib import Path;"
            "sys.stdout.buffer.write(Path('repo-marker').read_bytes()+"
            "(Path(os.environ['HOME'])/'home-marker').read_bytes()+sys.stdin.buffer.read())")
    argv = ["python3", "-c", code] if name == "docker" else ic.py(code)
    assert ic.run(backend.run(identity, ic.invocation(argv, stdin=snapshot), "execute")) == "execute"
    end = time.monotonic() + WAIT
    while ic.kind_of(ic.run(backend.inspect(identity))) != "exited" and time.monotonic() < end:
        time.sleep(0.02)
    assert ic.kind_of(ic.run(backend.inspect(identity))) == "exited"
    expected = b"checkout\nhome\n" + framed
    assert ic.data_of(ic.run(backend.output(identity, "execute", "stdout", 0))) == expected
    ic.run(backend.remove(identity, True))
    assert ic.run(backend.checkout_path(identity)) == checkout.resolve()
    assert ic.data_of(ic.run(backend.output(identity, "execute", "stdout", 0))) == expected
    ic.run(backend.remove(identity, False))
    assert ic.kind_of(ic.run(backend.checkout_path(identity))) == "absent"
