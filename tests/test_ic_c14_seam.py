"""IC-C14 / K17.3: typed provisioning seam on both public backends."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import fake_docker as fdk  # noqa: E402
import ic_backends  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import identity_of, kind_of  # noqa: E402


@pytest.fixture(params=["reference", "docker"])
def sb(request, tmp_path, monkeypatch):
    """(backend, root, name, world-or-None)."""
    if request.param == "reference":
        root = tmp_path / "root"
        root.mkdir()
        backend = ic_backends.make_reference(root)
        assert all(callable(getattr(backend, name, None)) for name in
                   ("checkout_path", "attach_checkout", "prepare_input")), "K17.3 seam is missing"
        return backend, root, "reference", None
    w = s3.DockerWorld(tmp_path, monkeypatch)
    return w.backend, w.root, "docker", w


def made(sb, key="seam-1"):
    backend, root, _, _ = sb
    return identity_of(ic.run(backend.provision(ic.launch_spec(root), key)))


def test_ic_c14_k15_checkout_path_is_the_hosts_directory_of_the_guest_repository(sb):
    backend, _, name, w = sb
    identity = made(sb)
    path = ic.run(backend.checkout_path(identity))
    assert isinstance(path, Path) and path.is_dir()
    if w is not None:                                   # on Docker it IS what the container mounts at guest_repo
        mount = [m for m in fdk.mounts_of(w.container(identity)) if m["destination"] == ic.GUEST_REPO]
        assert [Path(m["source"]).resolve() for m in mount] == [path.resolve()]


def test_ic_c14_k15_checkout_path_of_a_removed_resource_is_absent(sb):
    backend, *_ = sb
    identity = made(sb)
    ic.run(backend.remove(identity, False))
    assert kind_of(ic.run(backend.checkout_path(identity))) == "absent"


def test_ic_c14_k15_attach_checkout_redirects_checkout_path_and_returns_the_identity(sb, tmp_path):
    backend, root, *_ = sb
    identity = made(sb)
    target = root / "attached"
    target.mkdir()
    got = ic.run(backend.attach_checkout(identity, target))
    assert identity_of(got) == identity
    assert ic.run(backend.checkout_path(identity)).resolve() == target.resolve()


def test_ic_c14_k15_attach_checkout_of_an_absent_resource_is_absent(sb):
    backend, root, *_ = sb
    identity = made(sb)
    ic.run(backend.remove(identity, False))
    assert kind_of(ic.run(backend.attach_checkout(identity, root))) == "absent"


def test_ic_c14_k15_attach_checkout_is_refused_while_the_resource_is_live(sb):
    backend, root, name, w = sb
    identity = made(sb)
    if w is not None:
        w.fd.workload(runs=True)
    ic.run(backend.run(identity, ic.invocation(ic.py("import time; time.sleep(30)") if name == "reference"
                                                else ["/bin/sleep", "30"]), "inv-1"))
    try:
        got = ic.run(backend.attach_checkout(identity, root))
        assert kind_of(got) == "invalid_spec"
    finally:
        ic.run(backend.stop(identity, 0.2))


def test_ic_c14_k15_prepare_input_snapshots_the_prompt_into_a_host_file(sb):
    backend, root, *_ = sb
    identity = made(sb)
    path = ic.run(backend.prepare_input(identity, io.BytesIO(b"the prompt\n\x00\xff")))
    assert Path(path).read_bytes() == b"the prompt\n\x00\xff"
    assert Path(path).resolve().is_relative_to(root.resolve())


def test_ic_c14_k15_prepare_input_snapshots_are_independent(sb):
    backend, *_ = sb
    identity = made(sb)
    a = ic.run(backend.prepare_input(identity, io.BytesIO(b"one")))
    b = ic.run(backend.prepare_input(identity, io.BytesIO(b"two")))
    assert a != b and Path(a).read_bytes() == b"one" and Path(b).read_bytes() == b"two"


def test_ic_c14_k17_3_prepare_input_for_an_absent_resource_is_absent(sb):
    backend, *_ = sb
    identity = made(sb)
    ic.run(backend.remove(identity, False))
    assert kind_of(ic.run(backend.prepare_input(identity, io.BytesIO(b"x")))) == "absent"


def test_ic_c14_k17_3_attachment_is_idempotent_and_invalid_path_preserves_it(sb, tmp_path):
    backend, root, _, w = sb
    identity = made(sb)
    target = root / "checkout"
    target.mkdir()
    for _ in range(2):
        assert identity_of(ic.run(backend.attach_checkout(identity, target))) == identity
    outside = tmp_path / "unauthorised"
    outside.mkdir()
    got = ic.run(backend.attach_checkout(identity, outside))
    assert kind_of(got) == "invalid_spec"
    assert ic.run(backend.checkout_path(identity)) == target.resolve()
    assert kind_of(ic.run(backend.inspect(identity))) == "created"


def test_ic_c14_k17_3_input_source_stays_open_and_snapshot_survives_retention(sb):
    backend, _, _, _ = sb
    identity = made(sb)
    source = io.BytesIO(b"framed\x00\xff\n")
    snapshot = Path(ic.run(backend.prepare_input(identity, source)))
    assert snapshot.is_absolute() and not source.closed
    source.seek(0)
    source.write(b"changed")
    assert snapshot.read_bytes() == b"framed\x00\xff\n"
    ic.run(backend.remove(identity, True))
    assert snapshot.read_bytes() == b"framed\x00\xff\n"
    ic.run(backend.remove(identity, False))
    assert not snapshot.exists()


def test_k17_3_uncertain_docker_state_returns_unknown_without_reassociation(sb):
    backend, root, _, w = sb
    if w is None:
        pytest.skip("daemon uncertainty is a Docker operation")
    identity = made(sb)
    target = root / "alternate"
    target.mkdir()
    w.fd.daemon_down()
    for got in (ic.run(backend.checkout_path(identity)),
                ic.run(backend.attach_checkout(identity, target)),
                ic.run(backend.prepare_input(identity, io.BytesIO(b"prompt")))):
        assert kind_of(got) == "unknown"
    w.fd.daemon_down(False)
    assert ic.run(backend.checkout_path(identity)) != target
