"""QB1/QB3/QB4: script and Claude budget readings through public readers."""
import json
import math
import re
import shlex

import pytest
import c2_harness as h
from multiagents.executor.local import LocalExecutor


def reading(tmp_path, payload):
    text = json.dumps(payload)
    h.case_script(tmp_path, "p.sh", f"budget) printf %s {shlex.quote(text)}; exit 0 ;;")
    return h.read_provider("p", h.make_provider("p"), LocalExecutor(), tmp_path,
                           use_cache=False)


def test_qb1_cached_reads_decorate_once_and_recover_after_cooldown(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(h.budget_mod.time, "time", lambda: clock[0])
    script = h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known":true,"headroom":0.5,"note":"reading"}\'; exit 0 ;;')
    kwargs = dict(providers={"p": h.make_provider("p")},
                  executor_for=lambda name: LocalExecutor(), config_dir=tmp_path,
                  cooldowns={"p": {"until": clock[0] + 10, "reason": "rate limited"}})
    first = h.read_all(**kwargs)["p"]
    # A broken script proves subsequent reads are served within the TTL.
    script.write_text("#!/bin/sh\nexit 3\n")
    for _ in range(3):
        b = h.read_all(**kwargs)["p"]
        assert b.note == "reading | rate limited"
        assert b.severity == "critical"
        assert b.headroom == 0.5
    clock[0] += 10
    recovered = h.read_all(**kwargs)["p"]
    assert recovered.note == "reading"
    assert recovered.severity == "normal"
    assert recovered.cooldown_until is None
    assert recovered.headroom == 0.5
    assert first.note == "reading | rate limited"


@pytest.mark.parametrize("value", [-0.1, -0.000001, 1.000001, 1.5, 85, True, False,
                                   "0.5", "", float("nan"), float("inf"), -float("inf")])
def test_qb3_invalid_headroom_refuses_entire_reading(tmp_path, value):
    b = reading(tmp_path, {"known": True, "headroom": value,
                           "windows": {"five_hour": {"percent": 20}}})
    assert (b.known, b.headroom, b.severity, b.windows) == (False, None, "unknown", {})
    # Accept equivalent decimal/scientific spellings in the diagnostic.
    assert b.note, "a refused reading must explain the received value"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        numbers = re.findall(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", b.note)
        assert any(float(number) == value for number in numbers), b.note
    elif value == "":
        assert any(marker in b.note.lower() for marker in ('""', "''", "empty")), b.note
    else:
        assert str(value).lower() in b.note.lower(), b.note


@pytest.mark.parametrize("value", [0, 1, 0.5, None, "absent"])
def test_qb3_valid_and_unknown_headroom_keep_their_meaning(tmp_path, value):
    payload = {"known": value not in (None, "absent")}
    if value != "absent":
        payload["headroom"] = value
    b = reading(tmp_path, payload)
    assert b.headroom == (None if value == "absent" else value)
    assert b.known == payload["known"]
    assert b.source == "script"


@pytest.mark.parametrize("stdout", ["", " \n\t"])
def test_qb4_blank_success_explains_missing_reading(tmp_path, stdout):
    h.case_script(tmp_path, "p.sh", f"budget) printf %s {shlex.quote(stdout)}; exit 0 ;;")
    b = h.read_provider("p", h.make_provider("p"), LocalExecutor(), tmp_path, use_cache=False)
    assert b.known is False
    assert b.note == "script returned no reading"


def test_qb4_script_note_is_preserved(tmp_path):
    b = reading(tmp_path, {"known": False, "note": "subscription unavailable"})
    assert b.note == "subscription unavailable"


@pytest.mark.parametrize("case", ["no-cache", "failed-fetch", "unreadable-bucket"])
def test_qb4_claude_source_none_without_readable_usage(tmp_path, monkeypatch, case):
    profile = h.claude_profile_dir(tmp_path, cached_usage=(
        {"limits": [{"percent": "unreadable"}]} if case == "unreadable-bucket" else None))
    h.fake_claude_fetch(monkeypatch, None, "usage endpoint unreachable")
    b = h.budget_mod.read_claude(config_dir=profile, fetch=case == "failed-fetch")
    assert b.known is False
    assert b.source == "none"


def test_qb4_claude_readable_cache_names_actual_source(tmp_path):
    profile = h.claude_profile_dir(tmp_path, cached_usage={"limits": [{"percent": 0}]})
    b = h.budget_mod.read_claude(config_dir=profile, fetch=False)
    assert b.known and b.headroom == 1
    assert b.source == "cachedUsageUtilization"
