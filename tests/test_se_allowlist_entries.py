"""SE1 (F2) and SE2 (F11): malformed egress_allowlist entries.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE, and
Decisions 12 and 13. Black box through `DockerExecutor.allowlist_entries()` (accepted
hosts, diagnostics) and the filter `write_proxy_config` writes. A diagnostic
reads exactly `refused allowlist entry {entry!r}: {reason}`.

No Docker daemon and no network: both surfaces are pure file generation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from multiagents.executor.docker import DockerExecutor

sys.path.insert(0, str(Path(__file__).parent / "support"))
from st_docker import admits  # noqa: E402


# Today's wording, which SE2 keeps (steps 1 and 3).
BLANK_OR_CONTROL = "blank entry or control character in hostname"
IPV6 = "IPv6 addresses are unsupported"
INVALID_IDNA = "invalid IDNA hostname"
SINGLE_LABEL = "single label; use a registrable domain"
PUBLIC_SUFFIX = "public suffix; use a registrable domain"
# New reasons (SE1, SE2 step 2).
NOT_A_STRING = "not a string"
WHITESPACE = "contains whitespace"
PORT = "contains a port"
LEADING_DOT = "leading dot"

VALID = "a.example"


def entries(allowlist):
    return DockerExecutor({"egress_allowlist": allowlist}).allowlist_entries()


def diagnostic(entry, reason):
    return f"refused allowlist entry {entry!r}: {reason}"


def filter_lines(tmp_path, allowlist):
    directory = DockerExecutor({"egress_allowlist": allowlist}).write_proxy_config(
        tmp_path / "proxy")
    text = (directory / "filter").read_text()
    return text, [line for line in text.splitlines() if line.strip()]


def assert_refused_alone(entry, reason):
    allowed, diagnostics = entries([entry])
    assert allowed == [], f"{entry!r} must not be accepted"
    assert diagnostics == [diagnostic(entry, reason)]


# ---------------------------------------------------------------------------
# SE1 — F2: a non-string entry is refused, never raises
# ---------------------------------------------------------------------------

NON_STRINGS = [
    pytest.param(5, id="int"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(None, id="null"),
    pytest.param({}, id="empty-mapping"),
    pytest.param([], id="empty-list"),
    # Boundaries of our own: what YAML or a hand-built config can also hand us.
    pytest.param(0, id="zero"),
    pytest.param(4.5, id="float"),
    pytest.param({"a.example": None}, id="mapping-yaml-trailing-colon"),
    pytest.param(["a.example"], id="list-holding-a-valid-host"),
    pytest.param(b"a.example", id="bytes"),
]


@pytest.mark.parametrize("entry", NON_STRINGS)
def test_se1_a_non_string_entry_is_refused_as_not_a_string(entry):
    assert_refused_alone(entry, NOT_A_STRING)


@pytest.mark.parametrize("entry", NON_STRINGS)
def test_se1_the_other_entries_are_still_read_around_a_non_string(entry):
    allowed, diagnostics = entries([entry, VALID, "b.example.com"])
    assert allowed == [VALID, "b.example.com"]
    assert diagnostics == [diagnostic(entry, NOT_A_STRING)]
    allowed, diagnostics = entries([VALID, entry])
    assert allowed == [VALID]
    assert diagnostics == [diagnostic(entry, NOT_A_STRING)]


def test_se1_every_non_string_gets_its_own_diagnostic_in_entry_order():
    allowlist = [5, True, None, {}, [], VALID]
    allowed, diagnostics = entries(allowlist)
    assert allowed == [VALID]
    assert diagnostics == [diagnostic(e, NOT_A_STRING) for e in allowlist[:-1]]


def test_se1_the_filter_is_written_and_admits_only_the_valid_entry(tmp_path, capsys):
    """Through the real writer: no exception, the valid entry still opens,
    nothing the malformed ones could spell does, and the refusals are shown."""
    text, lines = filter_lines(tmp_path, [5, None, {"x.example": 1}, VALID])
    assert len(lines) == 1, text
    assert admits(text, VALID) and admits(text, "api." + VALID)
    for host in ("5", "None", "x.example", "example.com"):
        assert not admits(text, host), (host, text)
    err = capsys.readouterr().err
    assert diagnostic(5, NOT_A_STRING) in err
    assert diagnostic(None, NOT_A_STRING) in err


@pytest.mark.parametrize("config", [
    pytest.param({"egress_allowlist": None}, id="null"),
    pytest.param({"egress_allowlist": []}, id="empty"),
    pytest.param({}, id="absent"),
])
def test_se1_a_whole_null_or_absent_allowlist_stays_empty(config):
    """Unchanged: `egress_allowlist: null` is an empty list, not one entry."""
    assert DockerExecutor(config).allowlist_entries() == ([], [])


# ---------------------------------------------------------------------------
# SE1 — Decision 13: a whole egress_allowlist that is not a list
# ---------------------------------------------------------------------------

def whole_diagnostic(value):
    return f"refused egress_allowlist {value!r}: not a list"


NON_LISTS = [
    pytest.param("a.example", id="string"),
    pytest.param(5, id="int"),
    pytest.param(True, id="true"),
    pytest.param({"a.example": 1}, id="mapping"),
    # Our own boundaries, by the letter of Decision 13 ("a string, a number,
    # a boolean, a mapping"): the falsy ones are not null, so not empty.
    pytest.param("a.example b.example", id="string-with-two-hosts"),
    pytest.param("ab", id="string-whose-characters-are-not-hosts"),
    pytest.param("", id="empty-string"),
    pytest.param(0, id="zero"),
    pytest.param(False, id="false"),
    pytest.param({}, id="empty-mapping"),
    pytest.param(4.5, id="float"),
]


@pytest.mark.parametrize("value", NON_LISTS)
def test_se1_a_whole_non_list_allowlist_is_refused_as_a_whole(value):
    """One diagnostic for the whole value, no host allowed, no exception, and
    never iterated: no per-entry (or per-character) diagnostic follows."""
    assert entries(value) == ([], [whole_diagnostic(value)])


@pytest.mark.parametrize("value", [
    pytest.param("a.example", id="string"),
    pytest.param({"a.example": 1}, id="mapping"),
    pytest.param(5, id="int"),
])
def test_se1_a_whole_non_list_allowlist_opens_nothing_in_the_filter(tmp_path, capsys, value):
    """Through the real writer: written without raising, no line in the
    filter, the would-be host not admitted, and the refusal shown."""
    text, lines = filter_lines(tmp_path, value)
    assert lines == [], text
    for host in (VALID, "api." + VALID, "a", "5"):
        assert not admits(text, host), (host, text)
    assert whole_diagnostic(value) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# SE2 — F11 step 2: new raw-shape refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    " a.com", "a.com ", "a .com", "a .com",
    # Our own: other Unicode whitespace that is not a control character.
    " a.com", "a.com ", "　a.com", "a .com", "a .com",
    "  a.com  ",
])
def test_se2_whitespace_is_refused(entry):
    assert_refused_alone(entry, WHITESPACE)


@pytest.mark.parametrize("entry", [
    "b.com:443",
    "b.com:0", "b.com:8080", "b.com:65536", "B.COM:443", "1.2.3.4:80",
    "sub.b.com:1",
])
def test_se2_a_trailing_port_is_refused(entry):
    assert_refused_alone(entry, PORT)


@pytest.mark.parametrize("entry", [".c.com", "..c.com", ".C.COM", ".", "...", ".c.com."])
def test_se2_a_leading_dot_is_refused(entry):
    assert_refused_alone(entry, LEADING_DOT)


@pytest.mark.parametrize("entry", [
    " a.com", "a.com ", "a .com", "a .com", "b.com:443", ".c.com"])
def test_se2_a_refused_shape_never_reaches_the_filter(tmp_path, capsys, entry):
    """Refused, so absent from the filter, while a valid neighbour still
    opens; and the refusal is printed where today's refusals are."""
    text, lines = filter_lines(tmp_path, [entry, VALID])
    assert len(lines) == 1, text
    assert admits(text, VALID)
    bare = entry.strip().strip(".").replace(" ", "").replace(" ", "").split(":")[0]
    for host in (bare, "api." + bare, entry):
        assert not admits(text, host), (host, text)
    reason = {"b.com:443": PORT, ".c.com": LEADING_DOT}.get(entry, WHITESPACE)
    assert diagnostic(entry, reason) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# SE2 — unchanged: step 1 (today's raw refusals) and step 3 (normalisation)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["a\t.com", "a\n.com", "a\x7f.com", "a\x00.com"])
def test_se2_a_control_character_is_refused_as_today(entry):
    assert_refused_alone(entry, BLANK_OR_CONTROL)


@pytest.mark.parametrize("entry", ["a\t .com", " a\x01.com", "a.com \x7f", "\tb.com:443", "\x1f.c.com"])
def test_se2_control_character_wins_over_the_new_refusals(entry):
    """Step 1 before step 2: an entry with a control character and a space
    (or a port, or a leading dot) keeps today's control-character refusal."""
    assert_refused_alone(entry, BLANK_OR_CONTROL)


@pytest.mark.parametrize("entry", ["", "   ", " ", "  　", "\t"])
def test_se2_a_blank_entry_is_refused_as_today(entry):
    """Whitespace-only is blank (step 1), not "contains whitespace"."""
    assert_refused_alone(entry, BLANK_OR_CONTROL)


@pytest.mark.parametrize("entry", ["::1", "[::1]:443", "[::1]", "fe80::1", "2001:db8::1"])
def test_se2_ipv6_keeps_todays_refusal_and_is_never_called_a_port(entry):
    """`[::1]:443` is accepted today and must be refused; several `:` make it
    an IPv6 form, not a port, so today's IPv6 wording applies."""
    assert_refused_alone(entry, IPV6)


@pytest.mark.parametrize(("entry", "host"), [
    ("d.com.", "d.com"),
    ("D.COM.", "d.com"),
    ("bücher.de", "xn--bcher-kva.de"),
    ("BÜCHER.de.", "xn--bcher-kva.de"),
    ("xn--bcher-kva.DE", "xn--bcher-kva.de"),
    ("例え.jp", "xn--r8jz45g.jp"),
])
def test_se2_trailing_dot_and_idna_entries_are_still_accepted(entry, host):
    """Decision 12: nothing accepted today on purpose starts being refused."""
    assert entries([entry]) == ([host], [])


def test_se2_a_valid_entry_is_accepted_unchanged():
    assert entries([VALID, "Example.COM", "1.2.3.4"]) == (
        [VALID, "example.com", "1.2.3.4"], [])


@pytest.mark.parametrize(("entry", "reason"), [
    ("com", SINGLE_LABEL),
    ("co.uk", PUBLIC_SUFFIX),
    ("a..com", INVALID_IDNA),
    ("x" * 300, INVALID_IDNA),
])
def test_se2_todays_refusals_on_the_normalised_name_keep_their_wording(entry, reason):
    assert_refused_alone(entry, reason)


# ---------------------------------------------------------------------------
# SE2 — precedence: the first refusal wins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("entry", "reason"), [
    # Step 2 before step 3: the shape is reported, not what normalisation
    # would have made of it.
    (".com", LEADING_DOT),          # not "single label" or IDNA
    (".co.uk", LEADING_DOT),        # not "public suffix"
    ("com:443", PORT),              # not "single label"
    ("co.uk:443", PORT),            # not "public suffix"
    (" co.uk", WHITESPACE),         # not "public suffix"
    ("x " + "x" * 300, WHITESPACE),  # not IDNA
    ("1.1:80", PORT),               # not "non-canonical IPv4"
])
def test_se2_a_raw_shape_refusal_wins_over_a_normalised_name_refusal(entry, reason):
    assert_refused_alone(entry, reason)


@pytest.mark.parametrize(("entry", "reason"), [
    (" .c.com", WHITESPACE),
    ("a .com:443", WHITESPACE),
    (".c.com:443", PORT),
])
def test_se2_within_step_two_the_listed_order_decides(entry, reason):
    """Assumption: step 2 lists whitespace, then port, then leading dot, and
    "checked in this order, the first refusal wins" applies to that listing."""
    assert_refused_alone(entry, reason)


def test_se2_the_answer_is_the_same_when_asked_twice():
    executor = DockerExecutor({"egress_allowlist": [" a.com", 5, "b.com:443", VALID]})
    first = executor.allowlist_entries()
    assert executor.allowlist_entries() == first
    assert first == ([VALID], [diagnostic(" a.com", WHITESPACE),
                               diagnostic(5, NOT_A_STRING),
                               diagnostic("b.com:443", PORT)])


# ---------------------------------------------------------------------------
# SE4 — hostname shape (F11, adversary finding): after SE2's normalisation
# every label is 1-63 letters, digits or hyphens, no hyphen at either end,
# any `xn--` label decodes, the whole name at most 253 characters.
# ---------------------------------------------------------------------------

INVALID_HOSTNAME = "invalid hostname"
NON_CANONICAL_IPV4 = ("non-canonical IPv4 address or fragment; "
                      "use a whole dotted-quad decimal address")


def name_of_length(length, tld="com"):
    """A name of exactly `length` characters, every label valid (<= 63)."""
    labels, remaining = [], length - len(tld)
    while remaining > 0:
        size = min(63, remaining - 1)
        if remaining - 1 - size == 1:  # never leave a lone dot with no label
            size -= 1
        labels.append("a" * size)
        remaining -= size + 1
    name = ".".join(labels + [tld])
    assert len(name) == length and all(1 <= len(l) <= 63 for l in name.split("."))
    return name


@pytest.mark.parametrize("entry", [
    "a_b.com",
    "-a.com",
    "a-.com",
    "xn--a.com",
    name_of_length(254),
    "_dmarc.example.com",
    # The same faults deeper in the name, and the shapes the adversary used.
    "x.a_b.com",
    "x.-a.com",
    "x.a-.com",
    "A_B.COM",
    "api.github.com#ci",
    "x#.example.com",
    "a*.example.com",
    "a(b)c.example.com",
    "user@example.com",
    "example%20.com",
    "a/b.example.com",
    "a:b.example.com",
])
def test_se4_a_name_failing_the_label_shape_is_refused_as_invalid_hostname(entry):
    assert_refused_alone(entry, INVALID_HOSTNAME)


@pytest.mark.parametrize("entry", ["a" * 64 + ".com", "x." + "b" * 64 + ".com"])
def test_se4_a_64_character_label_keeps_todays_idna_wording(entry):
    """IDNA itself rejects a label over 63, so today's reason stays."""
    assert_refused_alone(entry, INVALID_IDNA)


@pytest.mark.parametrize(("entry", "host"), [
    (name_of_length(253), name_of_length(253)),
    # The 253 limit applies after one trailing dot is removed.
    (name_of_length(253) + ".", name_of_length(253)),
    ("a" * 63 + ".com", "a" * 63 + ".com"),
    ("a.com", "a.com"),
    ("a-b.example.com", "a-b.example.com"),
    ("0.example.com", "0.example.com"),
    ("1a.example.com", "1a.example.com"),
    ("xn--bcher-kva.de", "xn--bcher-kva.de"),
    ("münchen.example.com", "xn--mnchen-3ya.example.com"),
    ("BÜCHER.de", "xn--bcher-kva.de"),
])
def test_se4_names_inside_every_bound_are_still_accepted(entry, host):
    assert entries([entry]) == ([host], [])


def test_se4_an_ipv4_address_keeps_its_exact_match_with_no_subdomains(tmp_path, capsys):
    assert entries(["1.2.3.4"]) == (["1.2.3.4"], [])
    text, _ = filter_lines(tmp_path, ["1.2.3.4"])
    assert admits(text, "1.2.3.4")
    for stranger in ("x.1.2.3.4", "11.2.3.4", "1.2.3.45", "1.2.3.4.attacker.example",
                     "1x2.3.4"):
        assert not admits(text, stranger), stranger


@pytest.mark.parametrize(("entry", "reason"), [
    # IPv6 is decided before the shape: brackets and colons are not labels.
    ("[::1]", IPV6),
    ("[fe80::1]", IPV6),
    # Today's non-canonical IPv4 refusal comes before the shape too.
    ("a_b.1", NON_CANONICAL_IPV4),
    ("-a.255", NON_CANONICAL_IPV4),
    # The shape comes before single label ...
    ("a_b", INVALID_HOSTNAME),
    ("-com", INVALID_HOSTNAME),
    ("xn--a", INVALID_HOSTNAME),
    # ... and before public suffix (`*.ck` makes every `x.ck` a suffix).
    ("a_b.ck", INVALID_HOSTNAME),
    ("-a.ck", INVALID_HOSTNAME),
])
def test_se4_the_shape_refusal_sits_after_ipv6_and_ipv4_and_before_label_and_suffix(
        entry, reason):
    assert_refused_alone(entry, reason)


def test_se4_the_shape_refusal_comes_after_every_raw_shape_refusal():
    for entry, reason in (("a_b.com:443", PORT), (" a_b.com", WHITESPACE),
                          (".a_b.com", LEADING_DOT), ("a_b\x01.com", BLANK_OR_CONTROL)):
        assert_refused_alone(entry, reason)


SE4_FILTER_MIX = [
    "a.example",
    "1.2.3.4",
    name_of_length(253),
    "bücher.de",
    "a" * 63 + ".example.com",
    # Refused by SE4: none of these may reach the file.
    "api.github.com#ci",
    "x#.example.com",
    name_of_length(254),
    ".".join(["a" * 60] * 9) + ".com",
    "_dmarc.example.com",
    "a(b)c.example.com",
]
FILTER_LINE_LIMIT = 511  # tinyproxy reads a filter line with fgets(buf, 512)


def test_se4_every_filter_line_is_anchored_at_both_ends_and_admits_only_its_host(
        tmp_path, capsys):
    """Checked on the generated filter text: one line per accepted host, each
    a whole pattern as tinyproxy reads it (no comment `#`, no whitespace, not
    split by its line buffer), admitting exactly that host and, for a domain,
    its subdomains."""
    allowed, _ = entries(SE4_FILTER_MIX)
    assert allowed == ["a.example", "1.2.3.4", name_of_length(253),
                       "xn--bcher-kva.de", "a" * 63 + ".example.com"]
    text, lines = filter_lines(tmp_path, SE4_FILTER_MIX)
    assert len(lines) == len(allowed)
    for line in text.splitlines():
        assert line.strip(), f"blank line in filter: {text!r}"
        assert "#" not in line and not any(c.isspace() for c in line), line
        assert len(line.encode()) <= FILTER_LINE_LIMIT, len(line)
    for host in allowed:
        own = [line for line in lines if admits(line, host)]
        assert len(own) == 1, (host, own)
        line = own[0]
        refused = ["x" + host, "evil-" + host, host + "x", host + ".attacker.example",
                   host[1:], "attacker.example"]
        if host == "1.2.3.4":
            refused.append("x." + host)
        else:
            assert admits(line, "api." + host), (line, host)
        for stranger in refused:
            assert not admits(line, stranger), (line, stranger)
        # No other accepted host is admitted by this host's line.
        for other in allowed:
            if other != host:
                assert not admits(line, other), (line, other)
