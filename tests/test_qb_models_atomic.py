"""QB5/F143: publication preserves the old catalog on failure."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import stat

import pytest
import yaml
from multiagents import models
from multiagents.providers import Provider

WAIT_SECONDS = 3


def providers(model="new"):
    return {"p": Provider.from_dict("p", {"models": [{"id": model}]})}


@pytest.mark.parametrize("exists", [True, False])
def test_qb5_serialization_failure_preserves_target_and_cleans_temp(tmp_path, monkeypatch, exists):
    target = tmp_path / "models.yaml"
    old = b"models: {p: [{id: old}]}\n"
    if exists:
        target.write_bytes(old)
    before = set(tmp_path.iterdir())

    def fail(data, stream, **kwargs):
        stream.write("partial serialization\n")
        raise yaml.YAMLError("injected serialization failure")

    monkeypatch.setattr(models.yaml, "safe_dump", fail)
    with pytest.raises(yaml.YAMLError, match="injected serialization failure"):
        models.refresh_models(providers(), target)
    assert target.exists() == exists
    if exists:
        assert target.read_bytes() == old
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o755])
def test_qb5_existing_target_permission_bits_survive(tmp_path, mode):
    target = tmp_path / "models.yaml"
    target.write_text("models: {}\n")
    target.chmod(mode)
    result = models.refresh_models(providers(), target)
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert yaml.safe_load(target.read_text())["models"] == {"p": [{"id": "new"}]}
    assert result["counts"] == {"p": 1}
    assert set(tmp_path.iterdir()) == {target}


def test_qb5_concurrent_refreshes_publish_whole_catalogs(tmp_path, monkeypatch):
    target = tmp_path / "models.yaml"
    target.write_text("models: {p: [{id: old}]}\n")
    barrier = Barrier(2, timeout=WAIT_SECONDS)
    dump = yaml.safe_dump

    def interleaved(data, stream, **kwargs):
        # Both writes have begun; an observer must still see the old catalog.
        stream.write("# partially written\n")
        barrier.wait()
        assert yaml.safe_load(target.read_text()) == {"models": {"p": [{"id": "old"}]}}
        barrier.wait()
        return dump(data, stream, **kwargs)

    monkeypatch.setattr(models.yaml, "safe_dump", interleaved)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(models.refresh_models, providers(name), target)
                   for name in ("first", "second")]
        for future in futures:
            future.result(timeout=WAIT_SECONDS + 2)
    assert yaml.safe_load(target.read_text())["models"] in (
        {"p": [{"id": "first"}]}, {"p": [{"id": "second"}]})
    assert set(tmp_path.iterdir()) == {target}


def test_qb5_last_completed_publication_wins(tmp_path, monkeypatch):
    target = tmp_path / "models.yaml"
    target.write_text("models: {}\n")
    second_ready, release_second = Event(), Event()
    dump = yaml.safe_dump

    def ordered_dump(data, stream, **kwargs):
        result = dump(data, stream, **kwargs)
        if data["models"]["p"][0]["id"] == "second":
            second_ready.set()
            assert release_second.wait(WAIT_SECONDS), "first refresh did not finish"
        else:
            assert second_ready.wait(WAIT_SECONDS), "second refresh did not serialize"
        return result

    monkeypatch.setattr(models.yaml, "safe_dump", ordered_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        second = pool.submit(models.refresh_models, providers("second"), target)
        first = pool.submit(models.refresh_models, providers("first"), target)
        try:
            first.result(timeout=WAIT_SECONDS + 2)
            assert yaml.safe_load(target.read_text())["models"] == {"p": [{"id": "first"}]}
        finally:
            release_second.set()
        second.result(timeout=WAIT_SECONDS + 2)
    assert yaml.safe_load(target.read_text())["models"] == {"p": [{"id": "second"}]}
    assert set(tmp_path.iterdir()) == {target}
