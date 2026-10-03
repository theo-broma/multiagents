"""MQ-R3a/R4a: install probes never hold up a monitor refresh."""

import threading
import time

import pytest

from multiagents.monitor import snapshot as snap
from multiagents.manifest import ProbeResult

from test_c18_monitor_quota_panel import Panel, reading

pytestmark = pytest.mark.real_providers


def test_mq_r3a_a_sleeping_probe_keeps_windows_visible_and_is_deduplicated(
        tmp_path, monkeypatch):
    started, release, finished = (threading.Event() for _ in range(3))
    calls = []

    def probe(name, paths, context):
        calls.append((name, context))
        started.set()
        try:
            release.wait(5)
            return ProbeResult(state="verified")
        finally:
            finished.set()

    panel = Panel(tmp_path, monkeypatch,
                  {"slow": reading("slow", {"session": {"percent": 25}})},
                  kind="docker", host_has=False, probe=probe)
    try:
        before = time.monotonic()
        row = panel.view()["slow"]
        assert time.monotonic() - before < 0.5
        assert row["install_status"] == "checking…"
        assert row["available"] is True
        assert len(row["lines"]) == 1 and "session" in row["lines"][0]
        assert started.wait(1)
        for _ in range(5):
            before = time.monotonic()
            row = panel.view()["slow"]
            assert time.monotonic() - before < 0.5
            assert row["install_status"] == "checking…"
        assert calls == [("slow", "docker")]
    finally:
        release.set()
        assert finished.wait(2)


def test_mq_r3a_completed_probe_is_cached_until_its_ttl(tmp_path, monkeypatch):
    calls = []

    def probe(name, paths, context):
        calls.append((name, context))
        return ProbeResult(state="missing")

    panel = Panel(tmp_path, monkeypatch,
                  {"cached": reading("cached", {"weekly": {"percent": 40}})},
                  kind="docker", probe=probe)
    deadline = time.monotonic() + 2
    while panel.view()["cached"]["install_status"] != "not installed":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    for _ in range(10):
        row = panel.view()["cached"]
        assert row["install_status"] == "not installed"
        assert row["available"] is False
    assert calls == [("cached", "docker")]

    # Age this project's result, leaving the clock and other workers alone.
    key = (str(panel.paths.config), "cached")
    with snap._LINE_LOCK:
        timestamp, available, status = snap._PROBE_CACHE[key]
        monkeypatch.setitem(snap._PROBE_CACHE, key,
                            (timestamp - snap.PROBE_TTL - 1, available, status))
    deadline = time.monotonic() + 2
    while len(calls) < 2:
        panel.view()
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert calls == [("cached", "docker"), ("cached", "docker")]
