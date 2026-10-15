"""IC-C9/P1/P3/P6/P9: durable seed authority and guest-visible Git metadata."""
import asyncio
from pathlib import Path

import pytest

import ic_prov as ip
from ic_support import launch_spec
from multiagents.provision.seeds import SeedError


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = ip.HostRepo(tmp_path / "host")
    ids = ip.standard_world(repo)
    registry = ip.seed_registry(repo.path)
    ref = registry.build("attempt-own", ids["input"])
    return repo, ids, registry, ref


@pytest.mark.parametrize("field,value", [("checkout_commit", "0" * 40), ("roots", []),
                                        ("object_format", "sha256"), ("seed_version", 999),
                                        ("cache_key", "a" * 64), ("extra", "unrecorded")])
def test_p3_restoring_a_record_that_differs_from_issued_authority_is_refused(seeded, field, value):
    _, _, registry, ref = seeded
    record = registry.record(ref, "attempt-own")
    record[field] = value
    with pytest.raises(SeedError) as error:
        registry.restore(record, "attempt-own")
    assert error.value.code == "unauthorised"


def test_p1_backend_checks_seed_authority_before_creating_a_resource(seeded, tmp_path, monkeypatch):
    _, _, registry, ref = seeded
    backend = ip.wired_backend(tmp_path / "backend", registry)
    writes = []
    lifecycle = ip.lifecycle()
    write = lifecycle._write

    def record_write(path, record):
        writes.append(Path(path).name)
        return write(path, record)

    monkeypatch.setattr(lifecycle, "_write", record_write)
    spec = launch_spec(tmp_path / "backend", owner="another-attempt", seed=ref)
    reply = asyncio.run(backend.provision(spec, "foreign-key"))
    assert isinstance(reply, ip.lifecycle().InvalidSpec)
    assert isinstance(asyncio.run(backend.find("foreign-key")), ip.lifecycle().Absent)
    assert not list((backend.root / ".lifecycle").glob("*/resource.json"))
    assert "resource.json" not in writes, "unauthorised seed caused a resource to be published before refusal"


def test_p9_fetch_head_does_not_expose_a_host_path(seeded, tmp_path):
    repo, _, registry, ref = seeded
    backend = ip.wired_backend(tmp_path / "backend", registry)
    spec = launch_spec(tmp_path / "backend", owner="attempt-own", seed=ref)
    identity = asyncio.run(backend.provision(spec, "fetch-head-key"))
    assert isinstance(identity, ip.lifecycle().ResourceIdentity)
    checkout = asyncio.run(backend.checkout_path(identity))
    fetch_head = Path(checkout) / ".git" / "FETCH_HEAD"
    text = fetch_head.read_text() if fetch_head.exists() else ""
    assert str(repo.path) not in text
    assert str(registry.root) not in text
    assert str(tmp_path) not in text


def test_p6_completed_clone_retry_preserves_new_work_without_an_invocation(seeded, tmp_path):
    _, _, registry, ref = seeded
    backend = ip.wired_backend(tmp_path / "backend", registry)
    spec = launch_spec(tmp_path / "backend", owner="attempt-own", seed=ref)
    identity = asyncio.run(backend.provision(spec, "completed-key"))
    checkout = asyncio.run(backend.checkout_path(identity))
    marker = Path(checkout) / "work-in-progress"
    marker.write_text("must survive a provisioning retry")
    assert asyncio.run(backend.provision(spec, "completed-key")) == identity
    assert marker.read_text() == "must survive a provisioning retry"
