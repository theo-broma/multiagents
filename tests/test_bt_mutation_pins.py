"""BT1 (F151/F152/F153/F155/F156): observable budget boundary pins."""
import json
import math
import shlex

import pytest
import c2_harness as h
from multiagents import budget
from multiagents.executor.local import LocalExecutor


SEVERITIES = [(74, "normal"), (75, "warning"), (76, "warning"),
              (89, "warning"), (90, "critical"), (91, "critical")]


def script_reading(tmp_path, headroom):
    payload = json.dumps({"known": True, "headroom": headroom})
    return h.case_script(tmp_path, "p.sh", f"budget) printf %s {shlex.quote(payload)}; exit 0 ;;")


@pytest.mark.parametrize("headroom,expected", [(0.149, None), (0.15, "p"), (0.151, "p")])
def test_bt1_f151_reserve_admission(tmp_path, headroom, expected):
    reading = budget.Budget("p", known=True, headroom=headroom)
    assert budget.pick_instance(["p"], {"p": reading}, 0.15, {"p"}) == expected


@pytest.mark.parametrize("used,expected", SEVERITIES)
def test_bt1_f152_claude_severity(tmp_path, used, expected):
    profile = h.claude_profile_dir(tmp_path, cached_usage={"limits": [{"percent": used}]})
    reading = budget.read_claude(fetch=False, config_dir=profile)
    assert reading.known
    assert reading.severity == expected


@pytest.mark.parametrize("used,expected", SEVERITIES)
def test_bt1_f152_script_severity(tmp_path, used, expected):
    script_reading(tmp_path, (100 - used) / 100)
    reading = budget.read_provider("p", h.make_provider("p"), LocalExecutor(),
                                   tmp_path, use_cache=False)
    assert reading.known
    assert reading.severity == expected


@pytest.mark.parametrize("age,expected", [(59.999, 0.5), (60.0, 0.8), (60.001, 0.8)])
def test_bt1_f153_cache_ttl(tmp_path, monkeypatch, age, expected):
    # Only isolate shared caches; populate/read them exclusively through the
    # public reader. The fake clock exercises expiry without a real wait.
    monkeypatch.setattr(budget, "_cache", {})
    monkeypatch.setattr(budget, "_cache_source", {})
    clock = [2_000_000_000.0]
    monkeypatch.setattr(budget.time, "time", lambda: clock[0])
    script_reading(tmp_path, 0.5)
    provider, executor = h.make_provider("p"), LocalExecutor()
    assert budget.read_provider("p", provider, executor, tmp_path).headroom == 0.5
    script_reading(tmp_path, 0.8)
    clock[0] += age
    assert budget.read_provider("p", provider, executor, tmp_path).headroom == expected


@pytest.mark.parametrize("headroom,expected", [
    (math.nextafter(0.02, 0), False), (0.02, False),
    (math.nextafter(0.02, 1), True),
])
def test_bt1_f155_usable_exhaustion(headroom, expected):
    assert budget.Budget("p", known=True, headroom=headroom).usable is expected


def test_bt1_f156_stale_exhausted_reading_is_usable():
    assert budget.Budget("p", known=True, headroom=0, stale=True).usable


@pytest.mark.parametrize("offset,expected", [(-0.001, True), (0, True), (0.001, False)])
def test_bt1_f156_cooldown_ending_now(monkeypatch, offset, expected):
    now = 2_000_000_000.0
    monkeypatch.setattr(budget.time, "time", lambda: now)
    reading = budget.Budget("p", known=True, headroom=0.5, cooldown_until=now + offset)
    assert reading.usable is expected
