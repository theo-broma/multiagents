"""IC-C14 through the scheduler's public entry points, with the real Docker
backend over the fake `docker` CLI (nothing injected): the admission probe,
launch and its failure classes, the project's resource configuration (F32) and
the evidence each attempt records (K5).

The fake runs no workload; it replays the stdout it was scripted with
(`FakeDockerSched.fd.workload`). Guessed names: `ic_s3` (G-S3.*).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import fake_docker as fdk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402


@pytest.fixture
def fs(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    yield s
    s.close()


def block_text(s, node):
    return ic.text_of(s.blocked(node))


# ------------------------------------------------------------- the launch

def test_ic_c14_a_node_runs_in_one_container_and_finishes(fs):
    fs.start()
    node = fs.create()["id"]
    view = s3.wait_done(fs, node)
    assert view["state"] == "done", ic.text_of(view)[:600]
    (run,) = fs.runs(node)
    assert run["executor"]["kind"] == "isolated" and run["executor"]["backend"] == "docker"
    assert isinstance(run["executor"]["backend_version"], str) and run["executor"]["backend_version"]
    assert fs.fake.spawns() == 0                         # no provider process on the host: Docker ran it (fake)
    (container,) = fs.fd.containers().values()
    assert fs.fd.start_count(container["Id"]) == 1


def test_ic_c14_the_attempt_records_its_keys_and_the_immutable_container_id(fs):
    fs.start()
    node = fs.create()["id"]
    s3.wait_done(fs, node)
    ev = s3.evidence_of(fs, node)
    (container,) = fs.fd.containers().values()
    assert ev["kind"] == "isolated" and ev["backend"] == "docker"
    assert ev["identity"] == container["Id"] and s3.HEX64.match(ev["identity"])
    assert ev["creation_key"] and ev["invocation_key"] and ev["invocation_id"] == ev["invocation_key"]
    assert ev["spec_version"] == 1 and ev["spec_digest"]


def test_ic_c14_the_host_run_output_is_what_the_container_wrote(fs):
    fs.start()
    node = fs.create()["id"]
    s3.wait_done(fs, node)
    stdout, _ = fs.host_output(fs.runs(node)[0]["run_id"])
    assert stdout == s3.events_stdout()


def test_ic_c14_the_container_has_the_labels_the_host_will_list_it_by(fs):
    fs.start()
    node = fs.create()["id"]
    s3.wait_done(fs, node)
    (container,) = fs.fd.containers().values()
    assert container["Config"]["Labels"].get(ic.PROJECT_LABEL) == fs.slug


def test_ic_c14_every_container_command_of_a_launch_is_one_the_protocol_needs(fs):
    fs.start()
    node = fs.create()["id"]
    s3.wait_done(fs, node)
    assert fs.fd.unsupported() == [] and fs.fd.resources_created() == []


# ------------------------------------------------- admission (IC-C3 block)

def test_ic_c14_an_unreachable_daemon_blocks_the_node_before_any_attempt_and_creates_nothing(fs):
    fs.fd.daemon_down()
    fs.start()
    node = fs.create()["id"]
    fs.wait(lambda: "isolation_unavailable" in block_text(fs, node), what="the block")
    assert "daemon" in block_text(fs, node).lower() or "docker" in block_text(fs, node).lower()
    assert fs.runs(node) == [] and fs.fd.calls_of("create") == [] and fs.fake.spawns() == 0
    fs.fd.daemon_down(False)
    assert s3.wait_done(fs, node)["state"] == "done"


def test_ic_c14_a_missing_image_blocks_the_node_and_nothing_is_built_or_pulled(fs):
    fs.fd.set_images([])
    fs.start()
    node = fs.create()["id"]
    fs.wait(lambda: "isolation_unavailable" in block_text(fs, node), what="the block")
    assert "image" in block_text(fs, node).lower()
    assert fs.runs(node) == [] and fs.fd.calls_of("create") == []
    assert [c for c in fs.fd.commands() if c[:1] in (["build"], ["pull"]) or c[:2] == ["image", "pull"]] == []


@pytest.mark.parametrize("mode", ["userns", "rootless"])
def test_ic_c14_a_userns_remap_or_rootless_daemon_blocks_with_the_stated_reason(tmp_path, monkeypatch, mode):
    s = s3.FakeDockerSched(tmp_path, monkeypatch, userns=mode)
    try:
        s.start()
        node = s.create()["id"]
        s.wait(lambda: "isolation_unavailable" in block_text(s, node), what="the block")
        assert "isolation_unavailable: docker user namespace mode not supported" in block_text(s, node)
        assert s.fd.calls_of("create") == []
    finally:
        s.close()


# ------------------------------------------------ operational failures (TF)

def test_ic_c14_a_failed_create_is_class_launch_retried_isolated_and_then_succeeds(fs):
    fs.fd.rule("create", effect="fail", stderr="Error response from daemon: boom", times=1)
    fs.start()
    node = fs.create()["id"]
    view = s3.wait_done(fs, node, timeout=ic.WAIT + 4)
    assert view["state"] == "done"
    runs = fs.runs(node)
    assert len(runs) == 2 and {r["executor"]["kind"] for r in runs} == {"isolated"}
    assert any(c["class"] == "launch" and "isolation_unavailable" in ic.text_of(c) for c in ic.causes(view))
    assert fs.fake.spawns() == 0


def test_ic_c14_a_create_that_succeeded_but_was_not_reported_leaves_no_second_container_after_the_retry_ends(fs):
    fs.fd.rule("create", effect="after_effect", times=1)
    fs.start()
    node = fs.create()["id"]
    s3.wait_done(fs, node, timeout=ic.WAIT + 4)
    # the half-made container was found by its key and cleaned up or retained, never leaked as a stray running one
    assert all(not c["State"]["Running"] for c in fs.fd.containers().values())
    assert sum(fs.fd.start_count(c["Id"]) for c in fs.fd.containers().values()) == 1


# --------------------------------------------- resources from project.yaml

def test_ic_c14_the_default_resources_apply_to_each_attempts_container(fs):
    fs.start()
    s3.wait_done(fs, fs.create()["id"])
    (c,) = fs.fd.containers().values()
    host = c["HostConfig"]
    assert host["Memory"] in (6 * 10 ** 9, 6 * 2 ** 30) and host["PidsLimit"] == 4096 and host["NanoCpus"] == 0


def test_ic_c14_the_projects_resource_configuration_reaches_the_container(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.executor("isolated", isolated={"backend": "docker",
                                         "resources": s3.resources(memory="512m", pids=77, cpu_shares=512, cpus=1.5)})
        s.start()
        s3.wait_done(s, s.create()["id"])
        (c,) = s.fd.containers().values()
        host = c["HostConfig"]
        assert host["Memory"] == 512 * 2 ** 20 and host["PidsLimit"] == 77 and host["NanoCpus"] == 1_500_000_000 and host["CpuShares"] == 512
    finally:
        s.close()


def load_with_resources(tmp_path, monkeypatch, resources):
    import c3_harness as h
    h.as_root(monkeypatch)
    p = sc.Project(tmp_path)
    p.add_provider("acme")
    p.add_agent("worker", "acme", "acme/m1")
    p.project["executor"] = {"kind": "isolated", "isolated": {"backend": "docker", "resources": resources}}
    p.write()
    return config_mod.load(ProjectPaths(p.root), seed=False)


@pytest.mark.parametrize("key", ["memory", "pids", "cpu_shares", "cpus"])
@pytest.mark.parametrize("bad", [0, -1, "0", "-5g", 0.0, "garbage", [], {}])
def test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key(tmp_path, monkeypatch, key, bad):
    with pytest.raises(ValueError, match=rf"executor\.isolated\.resources\.{key}"):
        load_with_resources(tmp_path, monkeypatch, {key: bad})


@pytest.mark.parametrize("key,good", [("memory", "1g"), ("pids", 1), ("cpus", 0.5)])
def test_ic_c14_f32_the_smallest_positive_values_load(tmp_path, monkeypatch, key, good):
    cfg = load_with_resources(tmp_path, monkeypatch, {key: good})
    assert cfg.executor == "isolated"


def test_ic_c14_f32_unset_means_the_default(tmp_path, monkeypatch):
    assert load_with_resources(tmp_path, monkeypatch, {}).executor == "isolated"


def test_ic_c14_f32_a_null_resources_section_is_unset_not_zero(tmp_path, monkeypatch):
    assert load_with_resources(tmp_path, monkeypatch, None).executor == "isolated"


@pytest.mark.parametrize("key", ["memory", "pids", "cpu_shares", "cpus"])
def test_k17_1_null_resource_member_uses_default(tmp_path, monkeypatch, key):
    assert load_with_resources(tmp_path, monkeypatch, {key: None}).executor == "isolated"


def test_k17_1_configured_cli_is_an_executable_and_image_reaches_docker(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.fd.set_images(["example/workspace:pinned"])
        s.executor("isolated", isolated={"backend": "docker", "docker": {
            "cli": str(s.fd.docker), "image": "example/workspace:pinned"}})
        s.start()
        assert s3.wait_done(s, s.create()["id"])["state"] == "done"
        assert all(c["Config"]["Image"] == "example/workspace:pinned" for c in s.fd.containers().values())
    finally:
        s.close()


def test_k17_1_isolated_docker_never_inherits_legacy_image_or_resources(tmp_path, monkeypatch):
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.executor("isolated", isolated={"backend": "docker"},
                   docker={"image": "legacy/missing:image", "memory": "16m"})
        s.start()
        assert s3.wait_done(s, s.create()["id"])["state"] == "done"
        (container,) = s.fd.containers().values()
        assert container["Config"]["Image"] == "multiagents/workspace:latest"
        assert container["HostConfig"]["Memory"] in (6 * 10**9, 6 * 2**30)
    finally:
        s.close()
