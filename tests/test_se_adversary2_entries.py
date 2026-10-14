"""Adversary, second pass: SE1/SE2/SE4 — `allowlist_entries` and the filter.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE (SE1, SE2,
SE4; Decisions 12, 13). All pass today. The seeded fuzz pins the properties
the spec states for any `egress_allowlist` value; the last test kills a
mutation the existing suite lets survive.

Pure file generation: no Docker daemon, no network. Fuzz seeds: SEEDS below.
"""
from __future__ import annotations

import ipaddress
import random
import re

import pytest

from multiagents.executor.docker import DockerExecutor
from test_se_adversary_filter import tinyproxy_admits, tinyproxy_lines

SEEDS = [20261014, 7, 11]
ALPHABET = list("abz09-._#:/[]* \t\n\\$^()|+?{}AZ") + [
    "é", "ß", "İ", "​", "。", "．", " ",
    "\u0085", "\x7f", "\x00", "\ud800", "٣", "xn--", "K", "ﬁ",
    "⒈", "Ⓐ", "́", "\U0001F600", "᠎", " ", "　",
    "﻿", "­", "͏"]
HOST_CHARS = "abz09-."


def _entry(rng):
    roll = rng.random()
    if roll < 0.08:
        return rng.choice([5, True, None, {}, [], 1.5, {"a": 1}, ["a.com"], b"a.com"])
    if roll < 0.15:
        return ".".join(str(rng.randint(0, 300)) for _ in range(rng.randint(1, 5)))
    n = rng.randint(0, 40)
    if roll < 0.6:
        text = ("".join(rng.choice(HOST_CHARS) for _ in range(n))
                + rng.choice(["", "é", "⒈", "．", "X", "İ", "​"])
                + "".join(rng.choice(HOST_CHARS) for _ in range(rng.randint(0, 5))))
    else:
        text = "".join(rng.choice(ALPHABET) for _ in range(n))
    return text + rng.choice([".com", ".example.com", ".co.uk", ".", ".github.io", ""])


def _is_ipv4(host):
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return False


def _listed(host, allowed):
    host = host.lower()
    return any(host == a or (not _is_ipv4(a) and host.endswith("." + a)) for a in allowed)


def _probes(allowed):
    out = ["evil.com", "1.2.3.4", "x.1.2.3.4"]
    for a in allowed:
        out += [a, "x." + a, a + ".evil", "x" + a, a[1:], a[:-1], "1." + a,
                a.upper(), a + "x", "evil-" + a, a.replace(".", "x", 1),
                "q-." + a, "-q." + a]
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_se1_se2_se4_any_allowlist_admits_exactly_the_accepted_hosts(seed):
    rng = random.Random(seed)
    for _ in range(4000):
        entries = [_entry(rng) for _ in range(rng.randint(0, 4))]
        executor = DockerExecutor({"egress_allowlist": entries})
        allowed, diagnostics = executor.allowlist_entries()  # never raises
        text = executor._proxy_filter()
        assert len(allowed) + len(diagnostics) == len(entries), (seed, entries)
        for host in allowed:
            assert re.fullmatch(r"[a-z0-9.-]+", host), (seed, entries, host)
            if not _is_ipv4(host):
                labels = host.split(".")
                assert len(labels) >= 2 and len(host) <= 253, (seed, entries, host)
                assert all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in labels), (seed, entries, host)
        assert len(tinyproxy_lines(text)) == len(allowed), (seed, entries, text)
        for probe in _probes(allowed):
            assert tinyproxy_admits(text, probe) == _listed(probe, allowed), (
                seed, entries, allowed, probe)


def test_se2_a_name_ending_in_bare_0x_is_refused_as_non_canonical_ipv4():
    """WHATWG's IPv4 parser reads a last label `0x` as the number 0, so
    `a.0x` is not a domain. Kills `0x[0-9a-f]*` -> `0x[0-9a-f]+`."""
    allowed, diagnostics = DockerExecutor({"egress_allowlist": ["a.0x"]}).allowlist_entries()
    assert allowed == []
    assert diagnostics == ["refused allowlist entry 'a.0x': non-canonical IPv4 address "
                           "or fragment; use a whole dotted-quad decimal address"]
