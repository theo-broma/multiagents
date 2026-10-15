"""IC-C19: deferred findings at the public isolated and local boundaries."""
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
    assert any("image" in str(problem).lower() for problem in ic.run(w.backend.probe()))
    # K7 allows an operational exception here; the caller wraps it as Unknown.
    try:
        got = ic.run(w.backend.provision(w.spec(), "f64"))
    except Exception:
        got = None
    assert got is None or not hasattr(got, "id")
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


STOP_WAIT = 3


@pytest.mark.parametrize("identity_mismatch", [False, True], ids=["reaped", "identity-mismatch"])
def test_ic_c19_f70_stale_local_handle_never_signals_its_recorded_pid_or_group(
        tmp_path, monkeypatch, identity_mismatch):
    """Observe real syscalls without faking process execution or signals.

    Forging the public pid_start identity is authorised by the F70 clarification.
    """
    import asyncio
    import os
    import signal
    from multiagents.executor.local import LocalExecutor

    async def scenario():
        handle = await LocalExecutor().start(
            [sys.executable, "-c", "import signal; signal.pause()" if identity_mismatch else "pass"],
            tmp_path, dict(os.environ))
        pid = handle.pid
        real_kill, real_killpg = os.kill, os.killpg
        observed = []

        def kill(target, sig):
            observed.append(("pid", target, sig))
            return real_kill(target, sig)

        def killpg(target, sig):
            observed.append(("pgid", target, sig))
            return real_killpg(target, sig)

        try:
            if identity_mismatch:
                handle.pid_start = "0"  # impossible identity for this live process
            else:
                assert await asyncio.wait_for(handle.wait(), STOP_WAIT) == 0
            with monkeypatch.context() as spies:
                spies.setattr(os, "kill", kill)
                spies.setattr(os, "killpg", killpg)
                await asyncio.wait_for(handle.stop(grace=0.01), STOP_WAIT)
            assert not [(kind, target, sig) for kind, target, sig in observed
                        if target in (pid, -pid) and sig != 0], observed
        finally:
            if handle.returncode is None:
                try:
                    real_killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(handle.wait(), STOP_WAIT)

    asyncio.run(scenario())
