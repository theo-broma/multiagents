"""Adversary tests for write_proxy_config — mutations that survived the suite.

These tests attack the tinyproxy.conf generation and edge cases the existing
suite does not cover. Each test would FAIL if the corresponding mutation were
applied to the production code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


def test_tinyproxy_conf_contains_filter_default_deny_yes(tmp_path):
    """M1: FilterDefaultDeny Yes is what makes the proxy deny-all by default.
    Without it, the filter becomes a deny-list instead of an allow-list."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    conf = (target / "tinyproxy.conf").read_text()
    assert "FilterDefaultDeny Yes" in conf


def test_tinyproxy_conf_contains_filter_type_ere(tmp_path):
    """M2: The generated patterns use ERE syntax (|, +, ?, ()). The config
    must specify FilterType ere, not regex (BRE)."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    conf = (target / "tinyproxy.conf").read_text()
    assert "FilterType ere" in conf


def test_tinyproxy_conf_contains_filter_case_sensitive_off(tmp_path):
    """M3: DNS is case-insensitive. The proxy must match hosts case-insensitively."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    conf = (target / "tinyproxy.conf").read_text()
    assert "FilterCaseSensitive Off" in conf


def test_tinyproxy_conf_contains_filter_urls_off(tmp_path):
    """M4: The generated patterns match against the bare host, not the full URL.
    FilterURLs Off ensures tinyproxy matches only the host."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    conf = (target / "tinyproxy.conf").read_text()
    assert "FilterURLs Off" in conf


def test_tinyproxy_conf_filter_path_matches_actual_filter_file(tmp_path):
    """M5: The Filter directive in tinyproxy.conf must point to the same path
    where the filter file is actually written."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    conf = (target / "tinyproxy.conf").read_text()
    # The filter file is at target / "filter"
    assert (target / "filter").exists()
    # The config should reference a path that will be mounted at /etc/tinyproxy/filter
    # in the container. We check that the directive is present and consistent.
    assert 'Filter "/etc/tinyproxy/filter"' in conf


def test_null_egress_allowlist_does_not_crash(tmp_path):
    """M6: If egress_allowlist is explicitly None in the config, the function
    should handle it gracefully, not raise TypeError from list(None)."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=None)
    # This should not raise
    target = ex.write_proxy_config(tmp_path / "proxy")
    patterns = (target / "filter").read_text().strip().splitlines()
    # Filter out empty lines
    patterns = [p for p in patterns if p.strip()]
    assert patterns == []


def test_filter_file_ends_with_newline(tmp_path):
    """M7: The filter file should end with a newline for POSIX compliance."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=["example.com"])
    target = ex.write_proxy_config(tmp_path / "proxy")
    content = (target / "filter").read_text()
    assert content.endswith("\n")


# The three tests below pinned the F2 crash: a non-string entry raised
# AttributeError (or TypeError) out of the filter writer. SE1 (F2,
# context/specs/findings-c1-c2-part2.md, Part 2b) refuses such an entry with
# `not a string`, never raises, and keeps reading the other entries. Reversed
# deliberately, not weakened: each still checks the valid neighbour opens.

def test_allowlist_with_none_entry_is_refused_and_the_rest_still_read(tmp_path, capsys):
    """An entry that is None, inside an otherwise valid list (not M6's whole
    `egress_allowlist: null`)."""
    allowlist = ["example.com", None]
    # SE4 (F11) fixes no pattern text: the filter is the valid entry's alone.
    assert h.filter_patterns(tmp_path, allowlist) == h.filter_patterns(
        tmp_path / "ref", ["example.com"])
    assert "refused allowlist entry None: not a string" in capsys.readouterr().err
    assert h.allowlist_admits(tmp_path, allowlist, "api.example.com")


def test_allowlist_with_integer_entry_is_refused_and_the_rest_still_read(tmp_path, capsys):
    """An integer entry (a port written on its own line) is refused, not
    stringified into a pattern."""
    allowlist = ["example.com", 8080]
    # SE4 (F11) fixes no pattern text: the filter is the valid entry's alone.
    assert h.filter_patterns(tmp_path, allowlist) == h.filter_patterns(
        tmp_path / "ref", ["example.com"])
    assert "refused allowlist entry 8080: not a string" in capsys.readouterr().err
    assert not h.allowlist_admits(tmp_path, allowlist, "8080")


def test_allowlist_with_boolean_true_entry_is_refused_without_raising(tmp_path, capsys):
    """Boolean True (an int subclass, and what YAML makes of `yes`) is
    refused like any other non-string; alone, it leaves a deny-all filter."""
    allowlist = [True]
    assert h.filter_patterns(tmp_path, allowlist) == []
    assert "refused allowlist entry True: not a string" in capsys.readouterr().err
    for host in ("True", "true", "1", "example.com"):
        assert not h.allowlist_admits(tmp_path, allowlist, host)


def test_allowlist_with_empty_list_produces_empty_filter_file(tmp_path):
    """An empty allowlist should produce a filter file with no patterns."""
    ex = h.make_docker_executor(tmp_path, egress_allowlist=[])
    target = ex.write_proxy_config(tmp_path / "proxy")
    content = (target / "filter").read_text()
    # Should be just a newline or empty
    assert content.strip() == ""


def test_filter_patterns_function_returns_list_of_strings(tmp_path):
    """The filter_patterns harness function should return a list of strings."""
    patterns = h.filter_patterns(tmp_path, ["example.com", "test.org"])
    assert isinstance(patterns, list)
    assert all(isinstance(p, str) for p in patterns)


def test_allowlist_admits_returns_boolean(tmp_path):
    """The allowlist_admits harness function should return a boolean."""
    result = h.allowlist_admits(tmp_path, ["example.com"], "example.com")
    assert isinstance(result, bool)
    assert result is True

    result = h.allowlist_admits(tmp_path, ["example.com"], "evil.com")
    assert isinstance(result, bool)
    assert result is False
