"""NT-R1 (config validation), ntfy notifications, first round.

The `notify:` section of project.yaml is refused at load, the way the
scheduler's invalid settings are (`SchedulerConfigError`, a ValueError naming
the key and the `project.yaml:<line>` of the offending value). Token-file
problems are NOT load errors: they refuse *sending*, and are tested with the
tool and the CLI.

Assumption (also in the run report): the load error is a ValueError whose
message names the offending key and the file with its line, like the
scheduler's. Nothing else about the exception's type is relied on.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths, shipped_defaults_dir  # noqa: E402

BASE = {"ntfy_url": "http://127.0.0.1:8080", "topic": "multiagents-test"}


def load_with(tmp_path, monkeypatch, notify_yaml=None, *, notify=None, global_yaml=None):
    """Load a project whose project.yaml is `notify:` + the given text (or dict)."""
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    gdir = tmp_path / "gconf"
    gdir.mkdir(exist_ok=True)
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    if notify is not None:
        (paths.config / "project.yaml").write_text(yaml.safe_dump({"notify": notify}))
    elif notify_yaml is not None:
        (paths.config / "project.yaml").write_text(notify_yaml)
    if global_yaml is not None:
        (gdir / "project.yaml").write_text(global_yaml)
    return config_mod.load(paths, seed=False)


def refused(tmp_path, monkeypatch, **notify):
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, notify={**BASE, **notify})
    return str(raised.value)


# ---------------------------------------------------------------- accepted

def test_nt_r1_the_minimal_section_loads_and_keeps_its_values(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, notify=BASE)
    assert cfg.project["notify"]["ntfy_url"] == BASE["ntfy_url"]
    assert cfg.project["notify"]["topic"] == BASE["topic"]


def test_nt_r1_the_full_section_loads(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, notify={
        **BASE, "token_file": str(tmp_path / "no-such-token-yet"),
        "events": ["question", "held", "anomaly", "done"], "min_interval_seconds": 30})
    assert cfg.project["notify"]["min_interval_seconds"] == 30


def test_nt_r1_a_missing_token_file_is_not_a_load_error(tmp_path, monkeypatch):
    # The token file refuses *sending*, never the load: the config must load
    # so that `notify status` and a fix-and-retry still work.
    cfg = load_with(tmp_path, monkeypatch, notify={**BASE, "token_file": "~/does/not/exist"})
    assert cfg.project["notify"]["topic"] == BASE["topic"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "https://example.invalid",
                                 "https://example.invalid:8443", "https://example.invalid/prefix",
                                 "http://example.invalid/"])
def test_nt_r1_http_and_https_urls_with_a_host_are_accepted(tmp_path, monkeypatch, url):
    assert load_with(tmp_path, monkeypatch, notify={**BASE, "ntfy_url": url}) is not None


@pytest.mark.parametrize("topic", ["a", "A_b-9", "x" * 64, "multiagents-0123456789abcdef"])
def test_nt_r1_topics_inside_the_rule_are_accepted(tmp_path, monkeypatch, topic):
    cfg = load_with(tmp_path, monkeypatch, notify={**BASE, "topic": topic})
    assert cfg.project["notify"]["topic"] == topic


@pytest.mark.parametrize("events", [["question"], ["held", "done"], ["anomaly"],
                                    ["question", "held", "anomaly", "done"]])
def test_nt_r1_known_events_are_accepted(tmp_path, monkeypatch, events):
    assert load_with(tmp_path, monkeypatch, notify={**BASE, "events": events}) is not None


@pytest.mark.parametrize("value", [1, 0.5, 30, 3600])
def test_nt_r1_a_positive_interval_is_accepted_and_kept(tmp_path, monkeypatch, value):
    cfg = load_with(tmp_path, monkeypatch, notify={**BASE, "min_interval_seconds": value})
    assert cfg.project["notify"]["min_interval_seconds"] == value


# ---------------------------------------------------------------- absent: off

def test_nt_r1_with_no_section_the_config_loads_and_notify_is_off(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, notify_yaml="team: ''\n")
    assert not cfg.project.get("notify")


def test_nt_r1_the_shipped_defaults_carry_no_notify_section():
    data = yaml.safe_load((shipped_defaults_dir() / "project.yaml").read_text())
    assert "notify" not in data


# ---------------------------------------------------------------- refused

BAD_URLS = ["", "ftp://example.invalid", "example.invalid", "example.invalid:8080",
            "http://", "https:///only-a-path", "//example.invalid", "file:///etc/passwd",
            "javascript:alert(1)", "   ", 8080]


@pytest.mark.parametrize("url", BAD_URLS)
def test_nt_r1_a_url_that_is_not_http_with_a_host_is_refused(tmp_path, monkeypatch, url):
    assert "ntfy_url" in refused(tmp_path, monkeypatch, ntfy_url=url)


def test_nt_r1_a_missing_url_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, notify={"topic": "t"})
    assert "ntfy_url" in str(raised.value)


def test_nt_r1_a_missing_topic_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, notify={"ntfy_url": BASE["ntfy_url"]})
    assert "topic" in str(raised.value)


@pytest.mark.parametrize("topic", ["", "   ", "x" * 65, "x" * 200, "a b", "a/b", "a.b", "a,b",
                                   "café", "a\nb", "a\r\nb", "../x", "a?b", "a#b", "a%20b"])
def test_nt_r1_a_topic_outside_the_rule_is_refused(tmp_path, monkeypatch, topic):
    assert "topic" in refused(tmp_path, monkeypatch, topic=topic)


@pytest.mark.parametrize("events", [["bogus"], ["question", "bogus"], [""],
                                    ["done", 3]])
def test_nt_r1_an_unknown_event_is_refused(tmp_path, monkeypatch, events):
    assert "events" in refused(tmp_path, monkeypatch, events=events)


@pytest.mark.parametrize("value", [0, -1, -0.5, "30", "abc", "", True, float("nan"), [30], {}])
def test_nt_r1_a_non_positive_or_non_numeric_interval_is_refused(tmp_path, monkeypatch, value):
    assert "min_interval_seconds" in refused(tmp_path, monkeypatch, min_interval_seconds=value)


def test_nt_r1_an_inline_token_is_refused_and_never_echoed(tmp_path, monkeypatch):
    secret = "tk_inlineSECRET99887766"
    message = refused(tmp_path, monkeypatch, token=secret)
    assert "token" in message
    assert secret not in message


def test_nt_r1_an_inline_token_is_refused_even_beside_a_token_file(tmp_path, monkeypatch):
    message = refused(tmp_path, monkeypatch, token="tk_x", token_file=str(tmp_path / "f"))
    assert "token" in message


@pytest.mark.parametrize("section", ["yes", 3, ["a"], "https://example.invalid"])
def test_nt_r1_a_section_that_is_not_a_mapping_is_refused(tmp_path, monkeypatch, section):
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, notify_yaml=f"notify: {yaml.safe_dump(section, default_flow_style=True).strip()}\n")
    assert "notify" in str(raised.value)


# ---------------------------------------------------------------- where it is refused

def test_nt_r1_the_refusal_names_the_file_and_the_line_of_the_offending_key(tmp_path, monkeypatch):
    text = "notify:\n  ntfy_url: http://127.0.0.1:1\n  topic: bad topic\n"
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, notify_yaml=text)
    assert re.search(r"project\.yaml:3\b", str(raised.value)), str(raised.value)


def test_nt_r1_a_refusal_in_the_global_layer_names_the_global_file(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as raised:
        load_with(tmp_path, monkeypatch, global_yaml=(
            "notify:\n  ntfy_url: http://127.0.0.1:1\n  topic: ok\n  min_interval_seconds: 0\n"))
    assert re.search(r"gconf/project\.yaml:4\b", str(raised.value)), str(raised.value)


def test_nt_r1_a_valid_project_layer_over_a_valid_global_layer_loads(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, notify={**BASE, "topic": "from-project"},
                    global_yaml="notify:\n  ntfy_url: http://127.0.0.1:1\n  topic: from-global\n")
    assert cfg.project["notify"]["topic"] == "from-project"
