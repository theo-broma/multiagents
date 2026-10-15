"""IC-C14 — the Docker backend, against the fake `docker` CLI.

The backend is driven through the IC-C4 protocol only; Docker is the scriptable
fake of `tests/support/fake_docker.py`, found on PATH (D-S3.3). Assertions
read what the CONTAINER is, in Docker's own vocabulary (`docker inspect`
fields), never how the argv was spelled. Real-Docker acceptance is separate
(`test_ic_c14_docker_conformance.py`, SG_TEST_DOCKER=1): matching argv in the
fake is never acceptance.

Public names follow K17.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import fake_docker as fdk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import data_of, identity_of, kind_of  # noqa: E402

GRACE = 0.2
MUTATING = ("create", "start", "stop", "kill", "rm", "pull", "network", "volume", "run", "exec", "cp")


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = s3.DockerWorld(tmp_path, monkeypatch)
    yield world


def mutations(fd):
    return [c for c in fd.commands() if c and c[0] in MUTATING]


def as_epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import datetime
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def run_started(w, key="key-1", inv="inv-1", **over):
    spec, identity = w.identity(key, **over)
    _, got = w.invoke(identity, key=inv)
    assert got == inv, got
    return spec, identity


# ============================================================== probe

def test_ic_c14_probe_is_empty_when_docker_is_ready_and_has_no_side_effect(w):
    before = w.snapshot_dirs()
    assert list(ic.run(w.backend.probe())) == []
    assert mutations(w.fd) == []
    assert w.fd.containers() == {}
    assert w.snapshot_dirs() == before


def test_ic_c14_probe_reports_an_unreachable_daemon(w):
    w.fd.daemon_down()
    problems = list(ic.run(w.backend.probe()))
    assert problems and any("daemon" in p.lower() or "docker" in p.lower() for p in problems), problems
    assert mutations(w.fd) == []


@pytest.mark.parametrize("stderr", [
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    "Error response from daemon: context deadline exceeded",
    "permission denied while trying to connect to the Docker daemon socket"])
def test_ic_c14_probe_reports_every_form_of_daemon_failure_without_raising(w, stderr):
    w.fd.rule(effect="fail", stderr=stderr, code=1)
    assert list(ic.run(w.backend.probe()))


def test_ic_c14_probe_reports_the_configured_image_absent_and_does_not_build_or_pull(w):
    w.fd.set_images([])
    problems = list(ic.run(w.backend.probe()))
    assert problems and any("image" in p.lower() for p in problems), problems
    assert [c for c in w.fd.commands() if c[:1] in (["build"], ["pull"]) or c[:2] == ["image", "pull"]] == []
    assert mutations(w.fd) == []


@pytest.mark.parametrize("mode", ["userns", "rootless"])
def test_ic_c14_probe_refuses_a_userns_remap_or_rootless_daemon(tmp_path, monkeypatch, mode):
    world = s3.DockerWorld(tmp_path, monkeypatch, userns=mode)
    problems = list(ic.run(world.backend.probe()))
    assert any("docker user namespace mode not supported" in p for p in problems), problems


def test_ic_c14_probe_lists_every_problem_not_just_the_first(tmp_path, monkeypatch):
    world = s3.DockerWorld(tmp_path, monkeypatch, userns="userns", images=[])
    problems = list(ic.run(world.backend.probe()))
    text = " ".join(problems).lower()
    assert "image" in text and "user namespace" in text, problems


# ============================================================ provision

def test_ic_c14_provision_creates_one_container_and_does_not_start_it(w):
    spec, got = w.provision()
    identity = identity_of(got)
    assert s3.HEX64.match(identity.id), identity
    assert list(w.fd.containers()) == [identity.id]                 # the immutable id is the identity
    c = w.container(identity)
    assert c["State"]["Status"] == "created" and w.fd.start_count(identity.id) == 0
    assert [c for c in w.fd.commands() if c[:1] in (["start"], ["run"])] == []
    assert len(w.fd.calls_of("create")) == 1


def test_ic_c14_the_container_name_is_derived_from_the_key(tmp_path, monkeypatch):
    names = {}
    for tag, key in (("one", "key-a"), ("two", "key-a"), ("three", "key-b")):
        sub = tmp_path / tag
        sub.mkdir()
        world = s3.DockerWorld(sub, monkeypatch)
        names[tag] = world.container(world.identity(key)[1])["Name"]
    assert names["one"] == names["two"] != names["three"]
    assert names["one"].strip("/")


def test_ic_c14_the_container_carries_project_owner_lifetime_and_digest_labels(w):
    spec, identity = w.identity("key-1", owner="att_label1")
    labels = w.container(identity)["Config"]["Labels"]
    assert labels.get(ic.PROJECT_LABEL) == s3.PROJECT
    assert labels["multiagents.owner"] == "att_label1"
    assert labels["multiagents.lifetime"] == "attempt"
    assert labels["multiagents.creation_key"] == "key-1"
    assert labels["multiagents.spec_digest"] == ic.digest_of(spec)


def test_ic_c14_provision_is_idempotent_by_key(w):
    spec, first = w.provision("key-1")
    again = ic.run(w.backend.provision(spec, "key-1"))
    assert identity_of(again) == identity_of(first)
    assert len(w.fd.containers()) == 1


def test_ic_c14_the_same_key_with_another_digest_is_identity_conflict_and_changes_nothing(w):
    spec, identity = w.identity("key-1")
    before = w.fd.container(identity.id)
    other = w.spec(labels={"x": "y", ic.PROJECT_LABEL: s3.PROJECT})
    assert kind_of(ic.run(w.backend.provision(other, "key-1"))) == "identity_conflict"
    assert len(w.fd.containers()) == 1
    assert w.fd.container(identity.id)["Config"]["Labels"] == before["Config"]["Labels"]
    assert len(w.fd.calls_of("create")) == 1


def test_ic_c14_find_returns_what_was_created_under_the_key_and_absent_for_others(w):
    _, identity = w.identity("key-1")
    assert identity_of(ic.run(w.backend.find("key-1"))) == identity
    assert kind_of(ic.run(w.backend.find("never-used"))) == "absent"
    _, other = w.identity("key-2")
    assert identity_of(ic.run(w.backend.find("key-2"))) == other != identity


def test_ic_c14_a_crash_between_create_and_recording_the_id_is_recovered_by_find(w):
    w.fd.rule("create", effect="after_effect", times=1)            # created; the CLI never reported it
    spec = w.spec()
    try:
        first = ic.run(w.backend.provision(spec, "key-1"))
    except Exception as exc:                                       # an operational failure may raise
        first = exc
    created = list(w.fd.containers())
    assert len(created) == 1
    assert not hasattr(first, "id") or first.id == created[0]
    found = ic.run(w.backend.find("key-1"))
    assert identity_of(found).id == created[0]
    again = ic.run(w.backend.provision(spec, "key-1"))             # the retry finds, never duplicates
    assert identity_of(again).id == created[0]
    assert len(w.fd.containers()) == 1


def test_ic_c14_find_after_removal_is_absent(w):
    _, identity = w.identity("key-1")
    ic.run(w.backend.remove(identity, False))
    assert kind_of(ic.run(w.backend.find("key-1"))) == "absent"


def test_ic_c14_find_on_a_daemon_error_is_unknown_never_absent(w):
    w.identity("key-1")
    w.fd.rule("ps", effect="daemon_down")
    w.fd.rule("inspect", effect="daemon_down")
    assert kind_of(ic.run(w.backend.find("key-1"))) == "unknown"


# ---- F64: nothing happens before the image and the spec are checked

def test_ic_c14_f64_a_missing_image_means_no_side_effect_at_all(w):
    w.warm()
    before_dirs = w.snapshot_dirs()
    w.fd.set_images([])
    spec = w.spec()
    try:
        got = ic.run(w.backend.provision(spec, "key-1"))
    except Exception:
        got = None
    assert got is None or not hasattr(got, "id")
    assert w.fd.calls_of("create") == [] and w.fd.containers() == {} and w.fd.resources_created() == []
    assert w.snapshot_dirs() == before_dirs                        # no private storage made either
    assert kind_of(ic.run(w.backend.find("key-1"))) == "absent"


def test_ic_c14_f64_an_invalid_spec_means_no_side_effect_at_all(w):
    w.warm()
    before_dirs = w.snapshot_dirs()
    bad = w.spec(writable_paths=[str(w.tmp / "outside-the-root")])
    assert kind_of(ic.run(w.backend.provision(bad, "key-1"))) == "invalid_spec"
    assert w.fd.calls_of("create") == [] and w.fd.containers() == {}
    assert w.snapshot_dirs() == before_dirs


# ---- what the spec may not ask for (D-S3.2) and may not be

@pytest.mark.parametrize("field,value", [("credential_handle", "handle-1"), ("transport_handle", "handle-2"),
                                         ("session_state", "handle-3")])
def test_ic_c14_d_s3_2_a_handle_the_slice_does_not_implement_is_unsupported(w, field, value):
    w.warm()
    got = ic.run(w.backend.provision(w.spec(**{field: value}), "key-1"))
    assert kind_of(got) == "unsupported" and ic.field(got, "field") == field
    assert w.fd.calls_of("create") == [] and w.fd.containers() == {}


def test_ic_c14_d_s3_2_lifetime_conversation_is_unsupported_until_slice_6(w):
    got = ic.run(w.backend.provision(w.spec(lifetime="conversation"), "key-1"))
    assert kind_of(got) == "unsupported" and ic.field(got, "field") == "lifetime"
    assert w.fd.containers() == {}


# ---- the mounts, given and never given

def sources_under(root: Path, mounts):
    return all(Path(m["source"]).resolve().is_relative_to(root.resolve()) for m in mounts)


def test_ic_c14_the_attempts_private_storage_is_writable_at_the_guest_paths(w):
    _, identity = w.identity()
    c = w.container(identity)
    mounts = {m["destination"]: m for m in fdk.mounts_of(c)}
    for guest in (ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP):
        assert guest in mounts, sorted(mounts)
        assert mounts[guest]["rw"] is True and mounts[guest]["type"] == "bind"
        assert Path(mounts[guest]["source"]).resolve().is_relative_to(w.root.resolve())
    assert len({m["source"] for m in mounts.values()
                if m["destination"] in (ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP)}) == 3


def test_ic_c14_f31_private_storage_exists_before_create_owned_by_the_scheduler_uid_mode_0700(w):
    _, identity = w.identity()
    c = w.container(identity)
    by_path = {h["path"]: h for h in c["HostSources"]}
    for m in fdk.mounts_of(c):
        if m["destination"] in (ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP):
            seen = by_path[m["source"]]
            assert seen["exists"] is True, f"{m['source']} did not exist when the container was created"
            assert seen["uid"] == os.getuid() and seen["is_dir"] and seen["mode"] == 0o700, seen


def test_ic_c14_f31_every_mount_source_existed_at_creation(w):
    ro = w.root / "toolchain"
    ro.mkdir()
    _, identity = w.identity(readonly_paths=[str(ro)])
    assert all(h["exists"] for h in w.container(identity)["HostSources"])


def test_ic_c14_a_declared_toolchain_path_is_mounted_read_only(w):
    ro = w.root / "toolchain"
    ro.mkdir()
    (ro / "bin").write_text("x")
    _, identity = w.identity(readonly_paths=[str(ro)])
    mounted = [m for m in fdk.mounts_of(w.container(identity))
               if Path(m["source"]).resolve() == ro.resolve()]
    assert mounted and all(m["rw"] is False for m in mounted)


def test_ic_c14_f31_a_missing_toolchain_path_is_invalid_spec_naming_it_and_creates_nothing(w):
    w.warm()
    missing = w.root / "no-such-toolchain"
    got = ic.run(w.backend.provision(w.spec(readonly_paths=[str(missing)]), "key-1"))
    assert kind_of(got) == "invalid_spec" and "no-such-toolchain" in ic.field(got, "reason")
    assert w.fd.calls_of("create") == [] and w.fd.containers() == {}


def test_ic_c14_f35_a_symlink_leaving_the_root_is_refused_even_when_it_points_at_something_real(w):
    outside = w.tmp / "outside"
    outside.mkdir()
    (w.root / "link").symlink_to(outside)
    for field in ("readonly_paths", "writable_paths"):
        got = ic.run(w.backend.provision(w.spec(**{field: [str(w.root / "link")]}), "key-" + field))
        assert kind_of(got) == "invalid_spec", field
    assert w.fd.containers() == {}


def test_ic_c14_f35_a_path_with_dotdot_that_resolves_outside_the_root_is_refused(w):
    outside = w.tmp / "outside2"
    outside.mkdir()
    sneaky = str(w.root / ".." / "outside2")
    assert kind_of(ic.run(w.backend.provision(w.spec(readonly_paths=[sneaky]), "key-1"))) == "invalid_spec"
    assert w.fd.containers() == {}


def test_ic_c14_f35_a_toolchain_path_is_never_writable(w):
    ro = w.root / "toolchain"
    ro.mkdir()
    _, identity = w.identity(readonly_paths=[str(ro)])
    for m in fdk.mounts_of(w.container(identity)):
        if Path(m["source"]).resolve().is_relative_to(ro.resolve()):
            assert m["rw"] is False


def test_ic_c14_nothing_else_is_mounted_writable(w):
    extra = w.root / "extra"
    extra.mkdir()
    _, identity = w.identity(writable_paths=[str(extra)])
    writable = {m["destination"] for m in fdk.mounts_of(w.container(identity)) if m["rw"]}
    assert writable <= {ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP, str(extra), str(extra.resolve())}, writable
    assert {ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP} <= writable


def test_ic_c14_never_the_docker_socket_the_project_the_run_directory_or_the_whole_root(w):
    _, identity = w.identity()
    run_dir = w.tmp / "project" / ".multiagents" / "runs"
    run_dir.mkdir(parents=True)
    root = w.root.resolve()
    for m in fdk.mounts_of(w.container(identity)):
        assert "docker.sock" not in m["source"] and "docker.sock" not in m["destination"]
        source = Path(m["source"]).resolve()
        assert source != root and root.is_relative_to(source) is False, m      # not the root, not an ancestor
        assert source != w.tmp.resolve() and not (run_dir.resolve().is_relative_to(source))
    assert w.container(identity)["HostConfig"].get("Binds") is None or \
        all("docker.sock" not in b for b in w.container(identity)["HostConfig"]["Binds"])


def test_ic_c14_two_attempts_never_see_each_others_storage(w):
    _, a = w.identity("key-a", owner="att_a")
    _, b = w.identity("key-b", owner="att_b")
    ma = [Path(m["source"]).resolve() for m in fdk.mounts_of(w.container(a))]
    mb = [Path(m["source"]).resolve() for m in fdk.mounts_of(w.container(b))]
    for x in ma:
        for y in mb:
            assert not x.is_relative_to(y) and not y.is_relative_to(x), (x, y)


def test_ic_c14_only_host_issued_paths_under_the_root_back_any_mount(w):
    ro = w.root / "tool"
    ro.mkdir()
    _, identity = w.identity(readonly_paths=[str(ro)])
    assert sources_under(w.root, fdk.mounts_of(w.container(identity)))


# ---- process, user, network, namespaces

def test_ic_c14_no_network_no_added_capabilities_no_new_privileges(w):
    _, identity = w.identity()
    host = w.container(identity)["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert not host["CapAdd"] and host["Privileged"] is False
    assert any(opt.replace(":", "=").split("=")[0] == "no-new-privileges"
               and opt.replace(":", "=").split("=")[-1] != "false" for opt in host["SecurityOpt"] or []), \
        host["SecurityOpt"]
    assert not host.get("Devices")


def test_ic_c14_no_host_pid_ipc_uts_or_user_namespace(w):
    _, identity = w.identity()
    host = w.container(identity)["HostConfig"]
    assert host["PidMode"] not in ("host",) and not host["PidMode"].startswith("container:")
    assert host["IpcMode"] not in ("host",) and not host["IpcMode"].startswith("container:")
    assert host.get("UTSMode", "") != "host" and host["UsernsMode"] != "host"
    assert host["NetworkMode"] != "host"


def test_ic_c14_the_container_runs_as_the_scheduler_uid_and_gid_without_supplementary_groups(w):
    _, identity = w.identity()
    c = w.container(identity)
    assert c["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
    assert not c["HostConfig"]["GroupAdd"]


def test_ic_c14_the_restart_policy_is_no(w):
    _, identity = w.identity()
    assert w.container(identity)["HostConfig"]["RestartPolicy"]["Name"] in ("no", "")


def test_ic_c14_the_container_is_not_auto_removed_so_its_records_survive_exit(w):
    _, identity = w.identity()
    assert w.container(identity)["HostConfig"]["AutoRemove"] is False


def test_ic_c14_logging_is_pinned_at_creation_and_never_drops_data(w):
    _, identity = w.identity()
    c = w.container(identity)
    assert c["HostConfig"]["LogConfigPinned"] is True
    assert not fdk.may_lose_log_data(c), c["HostConfig"]["LogConfig"]


def test_ic_c14_default_resources_hard_memory_pids_equal_cpu_weight_no_cpu_cap(w):
    _, a = w.identity("key-a", owner="att_a")
    _, b = w.identity("key-b", owner="att_b")
    ha, hb = w.container(a)["HostConfig"], w.container(b)["HostConfig"]
    assert ha["Memory"] in (6 * 10 ** 9, 6 * 2 ** 30), ha["Memory"]
    assert ha["PidsLimit"] == 4096
    assert ha["CpuShares"] == hb["CpuShares"] == 1024                       # equal weight for every run
    assert ha["NanoCpus"] == 0                                      # no cap by default


# ---- no side channel: only the protocol's own commands, only the id

def test_ic_c14_the_backend_uses_no_exec_run_cp_network_or_volume(w):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, 0)
    ic.run(w.backend.inspect(identity))
    ic.run(w.backend.output(identity, "inv-1", "stdout", 0))
    ic.run(w.backend.stop(identity, GRACE))
    ic.run(w.backend.remove(identity, False))
    assert w.fd.unsupported() == [] and w.fd.resources_created() == []
    assert [c for c in w.fd.commands() if c[:1] in (["exec"], ["run"], ["cp"], ["attach"])] == []


def test_ic_c14_d_s3_4_every_inspect_signal_stop_and_removal_names_the_immutable_id(w):
    _, identity = w.identity("key-1")
    w.invoke(identity)
    ic.run(w.backend.inspect(identity))
    ic.run(w.backend.inspect_invocation(identity, "inv-1"))
    ic.run(w.backend.output(identity, "inv-1", "stdout", 0))
    ic.run(w.backend.stop(identity, GRACE))
    ic.run(w.backend.remove(identity, False))
    by_id = [c for c in w.fd.commands() if c[:1] in (["inspect"], ["start"], ["stop"], ["kill"], ["rm"], ["logs"], ["wait"])]
    assert by_id
    for command in by_id:
        targets = [a for a in command[1:] if not a.startswith("-")]
        # a value of an option (`-t 3`, `--format x`) is not a target; targets are the 64-hex ids
        ids = [t for t in targets if s3.HEX64.match(t)]
        names = [t for t in targets if t.strip("/") in (w.fd.container(identity.id) or {"Name": "/-"})["Name"]]
        assert ids == [identity.id] or not ids and command[0] == "rm", command
        assert not names, command


# ============================================================ run

def test_ic_c14_run_starts_the_container_once_and_returns_the_invocation_key(w):
    _, identity = w.identity()
    _, got = w.invoke(identity, key="inv-1")
    assert got == "inv-1"
    assert w.fd.start_count(identity.id) == 1
    assert kind_of(ic.run(w.backend.inspect(identity))) in ("running", "exited")


def test_ic_c14_the_invocation_spec_reaches_the_container_through_a_read_only_input_directory(w):
    _, identity = w.identity()
    w.invoke(identity, token="TOK-7f3a", env={"IC_ENV": "ENV-91bc"})
    inputs = [m for m in fdk.mounts_of(w.container(identity)) if m["rw"] is False]
    assert inputs, "no read-only input directory is mounted"
    found = ""
    for m in inputs:
        base = Path(m["source"])
        for f in ([base] if base.is_file() else sorted(base.rglob("*"))):
            if f.is_file():
                found += f.read_text(errors="ignore")
    assert "TOK-7f3a" in found and "ENV-91bc" in found
    assert ic.GUEST_REPO in found                                  # the working directory


def test_ic_c14_the_prompt_reaches_the_invocation_as_its_stdin_input_file(w):
    import io
    _, identity = w.identity()
    prepared = ic.run(w.backend.prepare_input(identity, io.BytesIO(b"PROMPT-5e21 hello\n")))
    _, got = w.invoke(identity, stdin=prepared)
    assert got == "inv-1"
    seen = ""
    for m in fdk.mounts_of(w.container(identity)):
        base = Path(m["source"])
        if m["rw"] is False and base.exists():
            for f in ([base] if base.is_file() else sorted(base.rglob("*"))):
                if f.is_file():
                    seen += f.read_text(errors="ignore")
    assert "PROMPT-5e21" in seen


def test_ic_c14_run_is_idempotent_by_key_and_never_starts_twice(w):
    _, identity = w.identity()
    spec, first = w.invoke(identity)
    again = ic.run(w.backend.run(identity, spec, "inv-1"))
    assert again == first == "inv-1"
    assert w.fd.start_count(identity.id) == 1


def test_ic_c14_run_with_the_same_key_and_another_spec_is_identity_conflict_and_starts_nothing(w):
    _, identity = w.identity()
    w.invoke(identity, token="one")
    other = ic.invocation(["/bin/echo", "two"], env={"IC_T": "two"})
    assert kind_of(ic.run(w.backend.run(identity, other, "inv-1"))) == "identity_conflict"
    assert w.fd.start_count(identity.id) == 1


def test_ic_c14_a_second_agent_invocation_on_an_attempt_resource_is_already_invoked(w):
    _, identity = w.identity()
    w.invoke(identity, key="inv-1")
    got = ic.run(w.backend.run(identity, ic.invocation(["/bin/true"]), "inv-2"))
    assert kind_of(got) == "already_invoked"
    assert w.fd.start_count(identity.id) == 1


def test_ic_c14_run_on_an_exited_attempt_resource_reports_exited_and_does_not_restart_it(w):
    _, identity = w.identity()
    w.invoke(identity, key="inv-1")
    w.fd.finish(identity.id, 0)
    got = ic.run(w.backend.run(identity, ic.invocation(["/bin/true"]), "inv-2"))
    assert kind_of(got) in ("exited", "already_invoked")
    assert w.fd.start_count(identity.id) == 1


def test_ic_c14_run_on_an_absent_resource_is_absent(w):
    _, identity = w.identity()
    ic.run(w.backend.remove(identity, False))
    assert kind_of(ic.run(w.backend.run(identity, ic.invocation(["/bin/true"]), "inv-1"))) == "absent"


def test_ic_c14_a_start_that_happened_but_was_not_reported_is_never_repeated_by_the_retry(w):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.fd.rule("start", effect="after_effect", times=1)             # started; the CLI died before answering
    spec = ic.invocation(["/bin/echo", "x"], env={"IC_T": "x"})
    try:
        first = ic.run(w.backend.run(identity, spec, "inv-1"))
    except Exception as exc:
        first = exc
    assert w.fd.start_count(identity.id) == 1
    retry = ic.run(w.backend.run(identity, spec, "inv-1"))
    assert retry == "inv-1"
    assert w.fd.start_count(identity.id) == 1                       # the invocation ran once


def test_ic_c14_a_failed_start_is_not_reported_as_started(w):
    _, identity = w.identity()
    w.fd.rule("start", effect="fail", stderr="Error response from daemon: cannot start", times=1)
    try:
        got = ic.run(w.backend.run(identity, ic.invocation(["/bin/true"]), "inv-1"))
    except Exception:
        got = None
    assert got != "inv-1"
    assert w.fd.start_count(identity.id) == 0
    assert kind_of(ic.run(w.backend.inspect(identity))) == "created"


# ============================================================ inspect

def test_ic_c14_inspect_reports_created_running_and_exited_from_dockers_records(w):
    _, identity = w.identity()
    assert kind_of(ic.run(w.backend.inspect(identity))) == "created"
    w.fd.workload(runs=True)
    w.invoke(identity)
    assert kind_of(ic.run(w.backend.inspect(identity))) == "running"
    assert kind_of(ic.run(w.backend.inspect_invocation(identity, "inv-1"))) == "running"
    w.fd.finish(identity.id, 7)
    for state in (ic.run(w.backend.inspect(identity)), ic.run(w.backend.inspect_invocation(identity, "inv-1"))):
        assert kind_of(state) == "exited"
        assert ic.field(state, "code") == 7 and ic.field(state, "oom") is False
        stamp = as_epoch(ic.field(state, "finished_at"))
        assert stamp is not None and abs(stamp - time.time()) < 300


@pytest.mark.parametrize("code,oom", [(0, False), (1, False), (137, True), (143, False), (255, False)])
def test_ic_c14_exit_code_and_oom_come_from_dockers_state(w, code, oom):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, code, oom=oom)
    state = ic.run(w.backend.inspect(identity))
    assert (kind_of(state), ic.field(state, "code"), ic.field(state, "oom")) == ("exited", code, oom)


def test_ic_c14_oom_is_docker_evidence_not_a_guess_from_the_exit_code(w):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, 137, oom=False)                        # killed by someone else: not OOM
    assert ic.field(ic.run(w.backend.inspect(identity)), "oom") is False


def test_ic_c14_a_removed_container_is_absent(w):
    _, identity = w.identity()
    w.fd.remove_behind_its_back(identity.id)
    assert kind_of(ic.run(w.backend.inspect(identity))) == "absent"
    assert kind_of(ic.run(w.backend.inspect_invocation(identity, "inv-1"))) == "absent"


DAEMON_FAILURES = [
    dict(effect="daemon_down"),
    dict(effect="fail", stderr="Error response from daemon: context deadline exceeded"),
    dict(effect="fail", stderr="Error response from daemon: 500 Internal Server Error"),
    dict(effect="fail", stderr="Error response from daemon: i/o timeout", code=125),
    dict(effect="garbage"),
    dict(effect="empty"),
    dict(effect="fail", stderr="", code=137),
]


@pytest.mark.parametrize("failure", DAEMON_FAILURES, ids=lambda f: f["effect"] + str(f.get("stderr", ""))[:20])
def test_ic_c14_a_daemon_error_or_garbage_is_unknown_never_absent_or_dead(w, failure):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.invoke(identity)
    w.fd.rule("inspect", **failure)
    for state in (ic.run(w.backend.inspect(identity)), ic.run(w.backend.inspect_invocation(identity, "inv-1"))):
        assert kind_of(state) == "unknown", state
        assert ic.field(state, "reason")


# ============================================================ output

def started_with_output(w, stdout=b"", stderr=b"", runs=False, code=0):
    _, identity = w.identity()
    w.fd.workload(exit_code=code, stdout=stdout, stderr=stderr, runs=runs)
    w.invoke(identity)
    return identity


def out(w, identity, stream="stdout", offset=0):
    return data_of(ic.run(w.backend.output(identity, "inv-1", stream, offset)))


def test_ic_c14_output_streams_are_separate_and_replayable_from_any_offset(w):
    identity = started_with_output(w, stdout=b"line one\nline two\n", stderr=b"warn\n")
    assert out(w, identity) == b"line one\nline two\n"
    assert out(w, identity, "stderr") == b"warn\n"
    for offset in range(len(b"line one\nline two\n") + 1):
        assert out(w, identity, offset=offset) == b"line one\nline two\n"[offset:]


def test_ic_c14_output_at_the_end_is_empty_bytes_not_a_failure(w):
    identity = started_with_output(w, stdout=b"abc\n")
    got = ic.run(w.backend.output(identity, "inv-1", "stdout", 4))
    assert data_of(got) == b""


def test_ic_c14_output_keeps_non_utf8_bytes_partial_lines_and_nul_exactly(w):
    blob = b"\xff\xfe binary \x00 tail-without-newline"
    identity = started_with_output(w, stdout=blob)
    assert out(w, identity) == blob
    assert out(w, identity, offset=5) == blob[5:]


def test_ic_c14_a_large_output_is_replayed_exactly(w):
    blob = b"".join(b"row %06d ........................................\n" % i for i in range(6000))
    identity = started_with_output(w, stdout=blob)
    assert out(w, identity) == blob
    assert out(w, identity, offset=123_457) == blob[123_457:]


def test_ic_c14_output_grows_while_running_and_each_offset_sees_what_was_written(w):
    identity = started_with_output(w, stdout=b"first\n", runs=True)
    assert out(w, identity) == b"first\n"
    w.fd.append_output(identity.id, stdout=b"second\n", stderr=b"e1\n")
    assert out(w, identity) == b"first\nsecond\n"
    assert out(w, identity, offset=6) == b"second\n"
    assert out(w, identity, "stderr") == b"e1\n"


def test_ic_c14_output_stays_readable_after_exit_and_after_a_retaining_remove(w):
    identity = started_with_output(w, stdout=b"kept\n", stderr=b"kept-err\n", code=3)
    ic.run(w.backend.remove(identity, True))
    assert out(w, identity) == b"kept\n" and out(w, identity, "stderr") == b"kept-err\n"


def test_ic_c14_a_daemon_error_while_reading_output_is_unknown_never_the_end_of_output(w):
    identity = started_with_output(w, stdout=b"data\n")
    w.fd.rule("logs", effect="daemon_down", times=1)
    got = ic.run(w.backend.output(identity, "inv-1", "stdout", 0))
    assert kind_of(got) == "unknown", got
    assert out(w, identity) == b"data\n"                            # and the retry reads it all


def test_ic_c14_output_never_depends_on_a_process_of_the_host(w):
    """Docker keeps the container's logs; the backend only reads them. There is
    no `attach` or follow stream a restart of the host would lose."""
    identity = started_with_output(w, stdout=b"x\n")
    fresh = s3.docker_backend(w.root)                                # a "restarted" host process
    assert data_of(ic.run(fresh.output(identity, "inv-1", "stdout", 0))) == b"x\n"


# ============================================================ stop

def test_ic_c14_stop_ends_the_container_and_is_confirmed_by_inspect(w):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.invoke(identity)
    got = ic.run(w.backend.stop(identity, GRACE))
    assert kind_of(got) == "exited"
    assert kind_of(ic.run(w.backend.inspect(identity))) == "exited"
    assert w.fd.container(identity.id)["State"]["Running"] is False


def test_ic_c14_stop_is_idempotent_and_absent_on_an_absent_resource(w):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.invoke(identity)
    assert kind_of(ic.run(w.backend.stop(identity, GRACE))) == "exited"
    assert kind_of(ic.run(w.backend.stop(identity, GRACE))) == "exited"
    ic.run(w.backend.remove(identity, False))
    assert kind_of(ic.run(w.backend.stop(identity, GRACE))) == "absent"


def test_ic_c14_stop_forces_termination_of_a_workload_that_ignores_the_graceful_signal(w):
    _, identity = w.identity()
    w.fd.workload(runs=True, ignore_term=True)
    w.invoke(identity)
    got = ic.run(w.backend.stop(identity, GRACE))
    assert kind_of(got) == "exited"
    assert w.fd.container(identity.id)["State"]["Running"] is False


def test_ic_c14_stop_that_cannot_be_confirmed_is_unknown(w):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.invoke(identity)
    w.fd.rule("stop", effect="empty")                               # the CLI says nothing and does nothing
    w.fd.rule("kill", effect="empty")
    got = ic.run(w.backend.stop(identity, GRACE))
    assert kind_of(got) == "unknown" and w.fd.container(identity.id)["State"]["Running"] is True


def test_ic_c14_stop_during_a_daemon_error_is_unknown(w):
    _, identity = w.identity()
    w.fd.workload(runs=True)
    w.invoke(identity)
    w.fd.rule(effect="daemon_down")
    assert kind_of(ic.run(w.backend.stop(identity, GRACE))) == "unknown"


# ============================================================ remove

def test_ic_c14_remove_keeping_artifacts_keeps_the_exited_container_and_its_storage(w):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, 0, stdout=b"o\n")
    storage = [Path(m["source"]) for m in fdk.mounts_of(w.container(identity))
               if m["destination"] in (ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP)]
    (storage[0] / "work.txt").write_text("kept")
    got = ic.run(w.backend.remove(identity, True))
    assert kind_of(got) == "exited"
    assert w.fd.container(identity.id) is not None and w.fd.calls_of("rm") == []
    assert all(p.exists() for p in storage) and (storage[0] / "work.txt").read_text() == "kept"
    assert kind_of(ic.run(w.backend.inspect(identity))) == "exited"
    assert identity in [identity_of(x) for x in ic.run(w.backend.list_owned(s3.PROJECT))]


def test_ic_c14_remove_without_artifacts_removes_the_container_and_the_private_storage(w):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, 0)
    storage = [Path(m["source"]) for m in fdk.mounts_of(w.container(identity))
               if m["destination"] in (ic.GUEST_REPO, ic.GUEST_HOME, ic.GUEST_TEMP)]
    got = ic.run(w.backend.remove(identity, False))
    assert kind_of(got) == "absent"
    assert w.fd.containers() == {} and not any(p.exists() for p in storage)
    assert ic.run(w.backend.list_owned(s3.PROJECT)) == []
    assert kind_of(ic.run(w.backend.find("key-1"))) == "absent"


def test_ic_c14_remove_is_idempotent(w):
    _, identity = w.identity()
    assert kind_of(ic.run(w.backend.remove(identity, False))) == "absent"
    assert kind_of(ic.run(w.backend.remove(identity, False))) == "absent"


def test_ic_c14_a_failed_removal_is_unknown_and_nothing_is_reported_clean(w):
    _, identity = w.identity()
    w.invoke(identity)
    w.fd.finish(identity.id, 0)
    w.fd.rule("rm", effect="daemon_down")
    got = ic.run(w.backend.remove(identity, False))
    assert kind_of(got) == "unknown"
    assert w.fd.container(identity.id) is not None


# ============================================================ list_owned (K12)

def test_ic_c14_list_owned_is_this_projects_containers_by_label_and_deletes_nothing(w):
    _, a = w.identity("key-a", owner="att_a")
    _, b = w.identity("key-b", owner="att_b")
    mine_unrecorded = w.fd.inject("orphan-1", {ic.PROJECT_LABEL: s3.PROJECT})
    w.fd.inject("other-project", {ic.PROJECT_LABEL: "some-other-project"})
    w.fd.inject("unlabelled", {})
    listed = [identity_of(x).id for x in ic.run(w.backend.list_owned(s3.PROJECT))]
    assert sorted(listed) == sorted([a.id, b.id, mine_unrecorded])
    assert w.fd.calls_of("rm") == [] and w.fd.calls_of("stop") == [] and w.fd.calls_of("kill") == []
    assert len(w.fd.containers()) == 5


def test_ic_c14_list_owned_never_lists_another_projects_containers(w):
    other = w.fd.inject("theirs", {ic.PROJECT_LABEL: "another-project"})
    w.identity("key-a")
    assert other not in [identity_of(x).id for x in ic.run(w.backend.list_owned(s3.PROJECT))]
    assert [identity_of(x).id for x in ic.run(w.backend.list_owned("another-project"))] == [other]


def test_ic_c14_list_owned_returns_ids_the_other_operations_accept(w):
    _, identity = w.identity()
    (listed,) = ic.run(w.backend.list_owned(s3.PROJECT))
    assert identity_of(listed) == identity and s3.HEX64.match(identity_of(listed).id)
    assert kind_of(ic.run(w.backend.inspect(identity_of(listed)))) == "created"


def test_ic_c14_k17_1_constructor_is_lazy_and_names_the_backend(tmp_path, monkeypatch):
    fd = fdk.FakeDocker(tmp_path).install(monkeypatch)
    root = tmp_path / "not-created"
    backend = s3.docker_backend(root, cli=str(fd.docker),
                                image="multiagents/workspace:latest", resources={"pids": 64})
    assert backend.name == "docker" and isinstance(backend.version, str) and backend.version
    assert not root.exists() and fd.commands() == []


@pytest.mark.parametrize("label", ["multiagents.owner", "multiagents.lifetime",
                                  "multiagents.creation_key", "multiagents.spec_digest"])
def test_ic_c14_k17_4_generated_labels_cannot_be_supplied(w, label):
    spec = w.spec(labels={ic.PROJECT_LABEL: s3.PROJECT, label: "forged"})
    assert kind_of(ic.run(w.backend.provision(spec, "reserved"))) == "invalid_spec"
    assert w.fd.containers() == {}


def test_ic_c14_k17_4_test_session_label_is_preserved(w):
    _, identity = w.identity(labels={ic.PROJECT_LABEL: s3.PROJECT, "multiagents.test.owner": "session-123"})
    assert w.container(identity)["Config"]["Labels"]["multiagents.test.owner"] == "session-123"


def test_k17_4_project_label_is_reserved_even_for_direct_callers(w):
    spec = w.spec(labels={"multiagents.project": "forged-project"})
    assert kind_of(ic.run(w.backend.provision(spec, "forged-project"))) == "invalid_spec"
    assert w.fd.containers() == {}


def test_k17_1_launch_resources_override_backend_defaults(tmp_path, monkeypatch):
    fd = fdk.FakeDocker(tmp_path).install(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    backend = s3.docker_backend(root, resources={"memory": "1g", "pids": 64, "cpu_shares": 256, "cpus": 2})
    spec = ic.launch_spec(root, resources={"memory": "128m", "pids": 32, "cpu_shares": 512, "cpus": 0.5})
    identity = identity_of(ic.run(backend.provision(spec, "overrides")))
    host = fd.container(identity.id)["HostConfig"]
    assert host["Memory"] == 128 * 2 ** 20 and host["PidsLimit"] == 32
    assert host["CpuShares"] == 512 and host["NanoCpus"] == 500_000_000
