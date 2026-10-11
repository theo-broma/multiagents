"""TF-R1/R3/R4: provider metadata and failure evidence remain distinct."""
from types import SimpleNamespace

import pytest
import yaml

from multiagents.failures import classify, parse_reset, pattern_lists, spawn_failure
from multiagents.paths import shipped_defaults_dir


@pytest.mark.parametrize("tag", ["SUCCESS", "OK", "COMPLETED", "DONE"])
@pytest.mark.parametrize("code", [-6, 1])
def test_tf_r1_success_metadata_does_not_hide_a_crash(tag, code):
    cause = classify(status="failed", exit_code=code,
                     final_status=tag, error_text=f"{tag}\n")
    assert cause["class"] == "crash"


def test_tf_r1_error_prose_still_controls_crash_classification():
    assert classify(status="failed", exit_code=1,
                    error_text="SUCCESS\ninvalid request")["class"] == "other"
    assert classify(status="failed", exit_code=1,
                    error_text="SUCCESS\nbackend unavailable",
                    patterns={"transient": ["backend unavailable"]})["class"] == "transient"


@pytest.mark.parametrize("source", [{"status": "session_lost"},
                                     {"final_status": "SESSION_LOST"},
                                     {"error_text": "SESSION_LOST\nsession not found"}])
def test_tf_r1_structured_session_loss_is_retryable(source):
    assert classify(**{"status": "failed", "exit_code": 1, **source})["class"] == "transient"


@pytest.mark.parametrize("delay,expected", [(None, "quota"), (0, "throttle"), (20, "throttle")])
def test_tf_r1_a_configured_quota_pattern_wins_over_a_429_without_delay(delay, expected):
    cause = classify(status="rate_limited", exit_code=1, retry_after=delay,
                     error_text="HTTP 429: custom account exhausted",
                     patterns={"quota": ["custom account exhausted"]})
    assert cause["class"] == expected


def test_tf_r1_quota_with_an_announced_retry_delay_is_throttle():
    cause = classify(status="rate_limited", error_text="usage limit exhausted; request will be retried")
    assert cause["class"] == "throttle"


@pytest.mark.parametrize("name,family,extends", [("opencode-zai", "", ""),
                                               ("zai-work", "opencode-zai", ""),
                                               ("zai-b", "", "opencode-zai")])
def test_tf_r3_zai_pattern_resets_use_utc_plus_eight(name, family, extends):
    block = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    provider = SimpleNamespace(name=name, family=family, extends=extends,
                               failures=block["providers"]["opencode-zai"]["failures"])
    text = "Usage limit reached for 5 hour. Your limit will reset at 2026-10-11 19:26:00."
    cause = classify(status="rate_limited", provider=name, patterns=pattern_lists(provider), error_text=text)
    assert cause["class"] == "quota"
    assert cause["reset_time"] == "2026-10-11T11:26:00+00:00"
    structured = classify(status="quota", provider=name, patterns=pattern_lists(provider),
                          error_text=text, resets_at="2026-10-11T20:26:00Z")
    assert structured["reset_time"] == "2026-10-11T20:26:00+00:00"


def test_tf_r3_generic_naive_pattern_resets_still_mean_utc():
    provider = SimpleNamespace(name="fake", family="fake", extends="",
                               failures={"reset": [r"reset at (?P<reset>.+)"]})
    cause = classify(status="quota", provider="fake", patterns=pattern_lists(provider),
                     error_text="reset at 2026-10-11 19:26:00")
    assert cause["reset_time"] == "2026-10-11T19:26:00+00:00"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10 ** 100, "not a timestamp"])
def test_tf_r3_invalid_reset_values_are_unknown(value):
    assert parse_reset(value) == ""


@pytest.mark.parametrize("errno,expected", [(2, "FileNotFoundError"), (13, "PermissionError"),
                                          (8, "OSError")])
def test_tf_r4_wrapper_spawn_markers_keep_the_exception_type(errno, expected):
    stderr = f"agentwrap: could not start /provider: [Errno {errno}] could not execute\n"
    assert spawn_failure(stderr).startswith(expected + ":")
    cause = classify(status="failed", launched=False, error_text=spawn_failure(stderr))
    assert cause["class"] == "launch"
    assert cause["exit_code"] is None


def test_tf_r4_a_quoted_wrapper_marker_is_not_launch_evidence():
    assert spawn_failure("provider says: agentwrap: could not start /provider") == ""


def test_tf_r1_excerpt_redaction_precedes_the_length_bound():
    secret = "sk-" + "a" * 40
    cause = classify(status="failed", error_text="api_key=" + secret + " " + "x" * 3000)
    assert len(cause["excerpt"]) == 2000
    assert secret not in cause["excerpt"]
    assert "[redacted]" in cause["excerpt"]
