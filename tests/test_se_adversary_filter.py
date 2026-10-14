"""Adversary: SE1/SE2 (F2, F11) — what the generated filter admits once
tinyproxy has read it.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE. Property
under attack: no `egress_allowlist` value yields a filter line that admits a
host not listed (the listed host or a subdomain of it).

`st_docker.admits` runs each line through Python's `re` as written. tinyproxy
does not see the file that way: its filter loader (filter.c) reads each line
with `fgets(buf, 512, ...)` and cuts the line at the first whitespace and at
the first `#` not preceded by a backslash (a comment). `tinyproxy_lines`
models exactly that, and the tests check admission against what tinyproxy
compiles.

Pure file generation: no Docker daemon, no network. Fuzz seed: SEED below.
"""
from __future__ import annotations

import random
import re

import pytest

from multiagents.executor.docker import DockerExecutor

SEED = 20261013
FILTER_BUFFER_LEN = 512  # tinyproxy filter.c


def tinyproxy_lines(text: str) -> list[str]:
    """The patterns tinyproxy compiles from a filter file."""
    out = []
    for line in text.splitlines():
        # fgets with a 512-byte buffer reads at most 511 bytes per call; the
        # remainder of a longer line comes back as the next "line".
        raw = line.encode()
        chunks = [raw[i:i + FILTER_BUFFER_LEN - 1]
                  for i in range(0, len(raw), FILTER_BUFFER_LEN - 1)] or [b""]
        for chunk in chunks:
            buf = chunk.decode("ascii", errors="replace")
            start = len(buf) - len(buf.lstrip())
            end = start
            while end < len(buf):
                char = buf[end]
                if char.isspace():
                    break
                if char == "#" and (end == 0 or buf[end - 1] != "\\"):
                    break
                end += 1
            pattern = buf[start:end]
            if pattern:
                out.append(pattern)
    return out


def generate(allowlist):
    executor = DockerExecutor({"egress_allowlist": allowlist})
    return executor.allowlist_entries(), executor._proxy_filter()


def tinyproxy_admits(filter_text: str, host: str) -> bool:
    for pattern in tinyproxy_lines(filter_text):
        try:
            if re.search(pattern, host, re.I):
                return True
        except re.error:
            continue
    return False


def listed(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allowed)


def witnesses(allowed: list[str]) -> list[str]:
    hosts = ["attacker.example", "x.attacker.example"]
    for a in allowed:
        hosts += [a + ".attacker.example", "attacker-" + a, a + "x",
                  a.split("#")[0] + ".attacker.example",
                  a.split("#")[0][:-1] + "q.attacker.example" if a.split("#")[0] else "q"]
    for pattern_host in allowed:
        head = pattern_host[:200]
        hosts.append(head + "-attacker.example")
    return hosts


# ---------------------------------------------------------------------------
# A `#` in an accepted entry cuts tinyproxy's line short
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry, stranger", [
    # The anchor `$` is behind the comment: "a.com" now matches as a prefix.
    ("api.github.com#ci", "api.github.com.attacker.example"),
    # Cut after one label: every host whose name starts with "x" is allowed.
    ("x#.example.com", "xfil.attacker.example"),
])
def test_se2_an_accepted_entry_never_lets_tinyproxy_admit_an_unlisted_host(entry, stranger):
    (allowed, _), text = generate([entry])
    if not allowed:
        return  # refused: nothing reaches the filter, the property holds
    assert not listed(stranger, allowed)
    assert not tinyproxy_admits(text, stranger), (
        f"egress_allowlist [{entry!r}] is accepted as {allowed}, and tinyproxy "
        f"reads the filter line {text.strip()!r} as {tinyproxy_lines(text)}, "
        f"which admits {stranger!r}")


def test_se2_a_long_accepted_entry_is_not_split_into_unanchored_lines():
    """Each label is valid (60 characters), so IDNA accepts the whole name;
    the filter line is longer than tinyproxy's line buffer."""
    entry = ".".join(["a" * 60] * 9) + ".com"
    (allowed, _), text = generate([entry])
    if not allowed:
        return
    first = tinyproxy_lines(text)[0]
    # What the first 511 bytes match literally, then a host of our own.
    literal = re.sub(r"\\(.)", r"\1", first[len("(^|\\.)"):]).rstrip("\\")
    stranger = literal + "zz.attacker.example"
    assert not listed(stranger, allowed)
    assert not tinyproxy_admits(text, stranger), (
        f"the {len(text.strip())}-character filter line becomes "
        f"{[p[:20] + '...' + p[-12:] for p in tinyproxy_lines(text)]} in tinyproxy, "
        f"which admits {stranger!r}")


# ---------------------------------------------------------------------------
# Seeded fuzz over every SE property at once
# ---------------------------------------------------------------------------

ALPHABET = list("abcz09-.:") + list("#*?+()[]{}|^$\\/%_@!~&=,;'\"`") + [
    " ", "\t", " ", " ", "　", "​", "﻿", "­",
    "。", "．", "｡", "а", "ß", "ẞ", "١",
    "²", "\x00", "\x7f", "\x85", "\udcff", "\U0001f600", "́",
]
ATOMS = ["com", "co.uk", "example", "xn--", "github", "1", "255", "0x1",
         "::1", "[", "]", ":443", "http://", "*.", ".."]


def random_entry(rng: random.Random):
    roll = rng.random()
    if roll < 0.05:
        return rng.choice([5, 1.5, True, False, None, {}, [], {"a": 1}, ["a.com"], b"a.com"])
    parts = []
    for _ in range(rng.randint(0, 6)):
        parts.append(rng.choice(ATOMS) if rng.random() < 0.4 else
                     "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 4))))
        parts.append(rng.choice([".", ".", "", "#"]) if rng.random() < 0.95 else ".")
    return "".join(parts)


def test_se_fuzz_refused_entries_never_reach_the_filter_and_nothing_unlisted_is_admitted():
    rng = random.Random(SEED)
    raising, leaked, admitted = [], [], []
    for _ in range(4000):
        allowlist = [random_entry(rng) for _ in range(rng.randint(0, 3))]
        try:
            (allowed, diagnostics), text = generate(allowlist)
        except Exception as exc:  # SE1: never raises
            raising.append((allowlist, repr(exc)))
            continue
        # Every entry either becomes one accepted host or one diagnostic.
        if len(allowed) + len(diagnostics) != len(allowlist):
            leaked.append(("count", allowlist, allowed, diagnostics))
        for host in witnesses(allowed):
            if tinyproxy_admits(text, host) and not listed(host, allowed):
                admitted.append((allowlist, allowed, host))
                break
    assert not raising, raising[:3]
    assert not leaked, leaked[:3]
    assert not admitted, (f"seed {SEED}: {len(admitted)} allowlists admit an unlisted "
                          f"host; first: {admitted[:3]}")


@pytest.mark.parametrize("value", [
    "a.example", "", 0, 1, False, True, {}, {"a.example": None}, 1.5,
    "x" * 100_000, ("a.example",),
])
def test_se1_a_whole_non_list_value_is_refused_whole_and_admits_nothing(value):
    (allowed, diagnostics), text = generate(value)
    assert allowed == []
    assert diagnostics == [f"refused egress_allowlist {value!r}: not a list"]
    assert tinyproxy_lines(text) == []


def test_se1_nested_and_self_referencing_entries_are_refused_without_raising():
    loop: list = []
    loop.append(loop)
    mapping: dict = {}
    mapping["k"] = mapping
    (allowed, diagnostics), _ = generate([loop, mapping, [["a.example"]], "a.example"])
    assert allowed == ["a.example"]
    assert [d.rsplit(": ", 1)[1] for d in diagnostics] == ["not a string"] * 3


# ---------------------------------------------------------------------------
# SE2 precedence on combinations, and the port rule's "digits"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry, reason", [
    ("\t.b.com:443", "blank entry or control character in hostname"),
    ("　", "blank entry or control character in hostname"),
    (" .b.com:443", "contains whitespace"),
    ("a b.com", "contains whitespace"),
    ("a\x85b.com", "contains whitespace"),
    (".b.com:443", "contains a port"),
    ("b.com.:443", "contains a port"),
    (".[::1]", "leading dot"),
    ("..b.com", "leading dot"),
    ("[::1]:443", "IPv6 addresses are unsupported"),
    ("::ffff:1.2.3.4", "IPv6 addresses are unsupported"),
    ("b.com::443", "IPv6 addresses are unsupported"),
    ("co.uk.", "public suffix; use a registrable domain"),
])
def test_se2_the_first_refusal_in_spec_order_wins(entry, reason):
    (allowed, diagnostics), text = generate([entry])
    assert allowed == []
    assert diagnostics == [f"refused allowlist entry {entry!r}: {reason}"]
    assert tinyproxy_lines(text) == []


@pytest.mark.parametrize("entry", ["b.com:https", "b.com:44a", "b.com:a44"])
def test_se2_a_colon_followed_by_non_digits_is_not_called_a_port(entry):
    _, diagnostics = generate([entry])[0]
    assert not any(d.endswith(": contains a port") for d in diagnostics)
