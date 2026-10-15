"""IC-C19: deferred findings at the public isolated boundary.

F70's PID-reassignment race needs an identity seam: a real test cannot force
kernel PID reuse safely and no process fake is authorised by D-S3.3.
"""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_s3 as s3
import ic_support as ic
from multiagents.paths import ProjectPaths


def test_ic_c19_f34_unusable_home_is_a_launch_failure_naming_the_path(tmp_path, monkeypatch):
    s = s3.S3Sched(tmp_path, monkeypatch)
    try:
        homes = ProjectPaths(s.root).homes
        homes.parent.mkdir(parents=True, exist_ok=True)
        homes.write_text("file blocks private HOMEs")
        s.start()
        node = s.node({"lines": 1})
        view = s3.wait_done(s, node)
        assert any(c.get("class") == "launch" and str(homes) in ic.text_of(c)
                   for c in ic.causes(view)), view
    finally:
        s.close()


def test_ic_c19_f31_missing_declared_toolchain_is_never_dropped(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    missing = w.root / "missing-toolchain"
    got = ic.run(w.backend.provision(w.spec(readonly_paths=[str(missing)]), "f31"))
    assert ic.kind_of(got) == "invalid_spec" and str(missing) in ic.text_of(got)
    assert w.fd.containers() == {}


def test_ic_c19_f32_zero_resource_is_rejected_with_full_config_key(tmp_path, monkeypatch):
    from test_ic_c14_scheduler_docker import load_with_resources
    with pytest.raises(ValueError, match=r"executor\.isolated\.resources\.pids"):
        load_with_resources(tmp_path, monkeypatch, {"pids": 0})


def test_ic_c19_f35_symlink_escape_cannot_authorise_a_host_mount(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = w.root / "escape"
    link.symlink_to(outside, target_is_directory=True)
    got = ic.run(w.backend.provision(w.spec(readonly_paths=[str(link)]), "f35"))
    assert ic.kind_of(got) == "invalid_spec" and w.fd.containers() == {}


def test_ic_c19_f64_missing_image_is_detected_before_creating_storage(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    w.fd.set_images([])
    before = sorted(p.relative_to(w.root) for p in w.root.rglob("*"))
    got = ic.run(w.backend.provision(w.spec(), "f64"))
    assert ic.kind_of(got) in ("unknown", "invalid_spec") and "image" in ic.text_of(got).lower()
    assert sorted(p.relative_to(w.root) for p in w.root.rglob("*")) == before
    assert w.fd.containers() == {}


def test_ic_c19_f65_credential_handle_refusal_creates_no_files(tmp_path, monkeypatch):
    w = s3.DockerWorld(tmp_path, monkeypatch)
    before = sorted(p.relative_to(w.root) for p in w.root.rglob("*"))
    got = ic.run(w.backend.provision(w.spec(credential_handle="secret-never-written"), "f65"))
    assert ic.kind_of(got) == "unsupported" and ic.field(got, "field") == "credential_handle"
    assert sorted(p.relative_to(w.root) for p in w.root.rglob("*")) == before


def test_ic_c19_f69_launch_never_deletes_a_host_provider_lock_using_its_pid(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        lock = Path.home() / ".config" / "opencode" / "SingletonLock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999999\n")
        s.fake.entry["container_private_reset"] = [".config/opencode/SingletonLock"]
        s.p.write()
        s._reset_server()
        s.start()
        assert s3.wait_done(s, s.create()["id"])["state"] == "done"
        assert lock.read_bytes() == b"999999999\n"
    finally:
        s.close()
