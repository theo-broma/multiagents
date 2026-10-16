"""Adversary pass, slice 3: container boundaries, config validation, reserved
labels and orphan handling, through the scheduler's public entry points with
the real Docker backend over the fake `docker` CLI (nothing injected).

Contract: context/specs/phase7-part2-contract.md, slice 3 (D-S3.2, IC-C14,
IC-C18, IC-C19 F65) and K17.1, K17.4, K17.5, K17.6.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import fake_docker as fdk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from ic_support import kind_of  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402


@pytest.fixture
def fs(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    yield s
    s.close()


def listing(s):
    reply = s.cli("scheduler", "orphans", timeout=ic.WAIT)
    assert reply.returncode == 0, reply.stderr
    return json.loads(reply.stdout)


def remove_orphan(s, cid):
    reply = s.cli("scheduler", "remove-orphan", cid, timeout=ic.WAIT)
    return reply.returncode, json.loads(reply.stdout)


# ================================== F65 / D-S3.2: no credential file, no copy

SECRET = "ic-adv-host-credential-7f3a"


def _host_credential(name: str) -> Path:
    path = Path(os.environ["HOME"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SECRET)
    return path


def _files_holding_secret(root: Path) -> list[str]:
    found = []
    for p in root.rglob("*"):
        try:
            if p.is_file() and SECRET in p.read_text(errors="replace"):
                found.append(str(p))
        except OSError:
            pass
    return found


@pytest.mark.parametrize("field", ["home_copy", "home_links"])
def test_adv_f65_an_isolated_attempt_writes_no_copy_or_link_of_a_host_credential(fs, field):
    """IC-C19 F65: an isolated attempt writes no credential file in slice 3.
    The provider declares it needs nothing (K17.2), yet its entry names a host
    credential file to bring into its HOME."""
    cred = _host_credential(".ic-adv-cred/token.json")
    fs.fake.entry[field] = [".ic-adv-cred/token.json"]
    fs.p.write()
    fs._reset_server()
    fs.start()
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    homes = ProjectPaths(fs.root).homes
    copies = _files_holding_secret(homes) if homes.exists() else []
    links = [str(p) for p in homes.rglob("*") if p.is_symlink() and p.resolve() == cred.resolve()] \
        if homes.exists() else []
    assert copies == [] and links == [], (copies, links)


# ======================================= K17.1 / F32: config vs the backend

def _load(tmp_path, monkeypatch, resources):
    import c3_harness as h
    h.as_root(monkeypatch)
    p = sc.Project(tmp_path)
    p.add_provider("acme")
    p.add_agent("worker", "acme", "acme/m1")
    p.project["executor"] = {"kind": "isolated", "isolated": {"backend": "docker", "resources": resources}}
    p.write()
    return config_mod.load(ProjectPaths(p.root), seed=False)


ACCEPTED_BY_CONFIG = [("memory", "512mb"), ("memory", "1t"), ("memory", ".5g"), ("memory", "6GB"),
                      ("pids", "4096"), ("cpu_shares", "512"), ("cpus", "1.5")]


@pytest.mark.parametrize("key,value", ACCEPTED_BY_CONFIG)
def test_adv_k17_1_a_resource_value_the_config_accepts_is_one_the_backend_launches(tmp_path, monkeypatch, key, value):
    """K17.1: a malformed value is a config error naming the key. A value the
    config loads must therefore not become an InvalidSpec at every launch."""
    try:
        _load(tmp_path / "cfg", monkeypatch, {key: value})
    except ValueError:
        pytest.skip("rejected at config load: consistent")
    w = s3.DockerWorld(tmp_path, monkeypatch)
    backend = s3.docker_backend(w.root, resources={key: value})
    got = ic.run(backend.provision(w.spec(), "key-res"))
    assert kind_of(got) not in ("invalid_spec", "unsupported", "unknown"), (key, value, got)


def test_adv_k17_1_a_config_loaded_string_pids_launches_through_the_scheduler(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.executor("isolated", isolated={"backend": "docker", "resources": {"pids": "4096"}})
        s.start()
        view = s3.wait_done(s, s.create()["id"])
        assert view["state"] == "done", ic.text_of(ic.causes(view))[:600]
    finally:
        s.close()


@pytest.mark.parametrize("bad", [True, False, float("nan"), float("inf"), "1e3", "nan", "inf", " 1g", "1g ",
                                 "1 g", "1gib", "0x10", "1_000"])
@pytest.mark.parametrize("key", ["memory", "pids", "cpu_shares", "cpus"])
def test_adv_k17_1_malformed_resource_values_are_refused_at_load_or_launch_never_passed_on(tmp_path, monkeypatch,
                                                                                        key, bad):
    """Either the config refuses the value naming the key, or the backend
    refuses it before any side effect; it never reaches `docker create`."""
    try:
        _load(tmp_path / "cfg", monkeypatch, {key: bad})
    except ValueError as exc:
        assert f"executor.isolated.resources.{key}" in str(exc)
        return
    w = s3.DockerWorld(tmp_path, monkeypatch)
    backend = s3.docker_backend(w.root, resources={key: bad})
    got = ic.run(backend.provision(w.spec(), "key-bad"))
    assert w.fd.calls_of("create") == [], (key, bad, got, w.fd.calls_of("create"))


# ===================================== K17.4: the digest the host recorded

def test_adv_k17_4_the_recorded_spec_digest_is_the_containers_label(fs):
    fs.start()
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    ev = s3.evidence_of(fs, node)
    (c,) = fs.fd.containers().values()
    labels = c["Config"]["Labels"]
    assert labels["multiagents.spec_digest"] == ev["spec_digest"]
    assert labels["multiagents.creation_key"] == ev["creation_key"]
    assert labels["multiagents.owner"] == fs.runs(node)[0]["attempt_id"]
    assert labels["multiagents.lifetime"] == "attempt"


# ================================================ K17.5 / K17.6: orphans

def _orphan(s, status="exited", **extra):
    return s.fd.inject("adv-unrecorded", {ic.PROJECT_LABEL: s.slug, "multiagents.owner": "att_x",
                                          "multiagents.lifetime": "attempt", **extra}, status=status)


def test_adv_k17_6_remove_orphan_never_reports_absent_for_a_container_that_still_exists(fs):
    """A project that has not run an isolated attempt yet (config `local`):
    `remove-orphan` must not answer `state: absent` for a container of this
    project that Docker still has."""
    fs.executor("local")
    fs.start()
    cid = _orphan(fs, status="running")
    code, value = remove_orphan(fs, cid)
    assert fs.fd.container(cid) is not None
    assert not (value.get("ok") is True and value.get("state") == "absent"), value


def test_adv_k17_5_orphans_of_a_project_not_configured_isolated_are_not_an_empty_success(fs):
    fs.executor("local")
    fs.start()
    cid = _orphan(fs)
    value = listing(fs)
    assert value["orphans_error"] is not None or any(o["container_id"] == cid for o in value["orphans"] or []), value


def test_adv_k17_6_an_unconfirmed_stop_of_a_live_orphan_deletes_nothing_and_it_stays_reported(fs):
    fs.start()
    cid = _orphan(fs, status="running")
    fs.fd.rule("stop", effect="fail", stderr="Error response from daemon: stuck")
    fs.fd.rule("kill", effect="fail", stderr="Error response from daemon: stuck")
    code, value = remove_orphan(fs, cid)
    assert code != 0 and value.get("ok") is False and value.get("error") == "unknown", value
    assert fs.fd.container(cid) is not None and fs.fd.calls_of("rm") == []
    fs.fd.clear_rules()
    assert any(o["container_id"] == cid for o in listing(fs)["orphans"])


def test_adv_k17_6_storage_named_by_an_orphans_labels_is_never_deleted(fs, tmp_path):
    fs.start()
    bait = tmp_path / "bait-storage"
    (bait / "repo").mkdir(parents=True)
    (bait / "repo" / "keep.txt").write_text("keep")
    cid = _orphan(fs, **{"multiagents.storage": str(bait), "multiagents.repo": str(bait / "repo"),
                         "multiagents.creation_key": "resource-adv-bait"})
    code, value = remove_orphan(fs, cid)
    assert code == 0 and value == {"ok": True, "container_id": cid, "state": "absent"}, value
    assert (bait / "repo" / "keep.txt").read_text() == "keep"


def test_adv_k17_6_an_orphan_claiming_a_recorded_attempts_creation_key_is_not_removed_as_unrecorded(fs):
    """A container with this project's label that carries the creation key of
    a retained, recorded attempt: discovery must not classify it while the key
    is ambiguous, and removal must not take the recorded attempt's container."""
    fs.start()
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    ev = s3.evidence_of(fs, node)
    recorded = ev["identity"]
    impostor = _orphan(fs, **{"multiagents.creation_key": ev["creation_key"]})
    code, value = remove_orphan(fs, recorded)
    assert fs.fd.container(recorded) is not None, value
    code, value = remove_orphan(fs, impostor)
    assert fs.fd.container(recorded) is not None, value


def test_adv_f65_an_alias_activations_guest_home_holds_no_copy_of_a_host_credential(tmp_path, monkeypatch):
    """The alias HOME is attached to the isolated resource as its guest HOME
    (K17.3 `attach_checkout(..., home)`), so a credential copied into it by
    HOME preparation is readable by the workload. Reference backend (K3)."""
    import ic_prov as ip
    from nc_fixture.m4_world import commit_entry, verdict_entry
    _host_credential(".ic-adv-cred/token.json")
    w = ip.make_world(tmp_path, monkeypatch)
    try:
        w.pair()
        w.fxr.entry["home_copy"] = [".ic-adv-cred/token.json"]
        w.fxw.queue(commit_entry("a.txt", "one\n", "r1"))
        w.fxr.queue(verdict_entry("approved", tag="R1", report=True))
        top, wk, rv = ip.start_pair(w, rounds=1)
        rep = w.report(w.fxr, "R1")
        home = Path(rep["home"])
        assert w.runs(rv) and w.runs(rv)[0]["executor"]["kind"] == "isolated"
        seen = _files_holding_secret(home)
        assert seen == [], (rep.get("home_files"), seen)
    finally:
        w.close()


# ================== IC-C14: what the guest is told, against what it can reach

def _guest_invocation(s):
    """What the guest reads at its read-only input mount (IC-C14 / D-S3.4)."""
    (c,) = s.fd.containers().values()
    mounts = {m["destination"]: m for m in fdk.mounts_of(c)}
    source = Path(mounts["/multiagents-input"]["source"])
    return json.loads((source / "invocation.json").read_text()), mounts


def _reachable(path: str, mounts) -> bool:
    return any(path == d or path.startswith(d.rstrip("/") + "/") for d in mounts)


def test_adv_ic_c14_the_guests_xdg_directories_lie_in_its_writable_home(fs):
    fs.start()
    assert s3.wait_done(fs, fs.create()["id"])["state"] == "done"
    invocation, mounts = _guest_invocation(fs)
    env = invocation["env"]
    assert env["HOME"] == ic.GUEST_HOME
    wrong = {k: env[k] for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
             if k in env and not _reachable(env[k], [ic.GUEST_HOME])}
    assert wrong == {}, wrong


def test_adv_ic_c14_the_guests_argv_names_no_host_checkout_path(fs):
    fs.start()
    node = fs.create()["id"]
    assert s3.wait_done(fs, node)["state"] == "done"
    invocation, mounts = _guest_invocation(fs)
    host_roots = [str(ProjectPaths(fs.root).worktrees), str(fs.root)]
    host_named = [a for a in invocation["argv"][1:]
                  if any(a == r or a.startswith(r.rstrip("/") + "/") for r in host_roots)
                  and not _reachable(a, mounts)]
    assert host_named == [], (invocation["argv"], invocation["cwd"])
