"""R13 — the directives that decide what the filter patterns *mean*.

The contract is `context/specs/phase2-proxy-directives.md`. `write_proxy_config`
generates two things: a set of ERE filter patterns, and the tinyproxy
directives that decide how those patterns are read and whether a host matching
none of them is refused. The 48-test characterization suite pins the patterns
between 9 and 43 times over; F50–F54 are what happens to the directives.

**What this file adds that `test_adversary_allowlist_mutation.py` does not.**
That file — the adversary run that *found* these defects — already carries five
guards, and a plain deletion of a directive goes red there today. They are
substring searches over the whole config, and a substring search is defeated by
three mutations that each leave the directive with no effect at all:

    'FilterDefaultDeny Yes' in '# FilterDefaultDeny Yes'        -> True
    'FilterDefaultDeny Yes' in 'XFilterDefaultDeny Yes'         -> True
    'FilterDefaultDeny Yes' in 'FilterDefaultDeny Yes\\nFilterDefaultDeny No'
                                                                -> True

Commented out, keyword misspelled so tinyproxy ignores the line, contradicted
by a later line that wins. So every assertion here goes through
`h.proxy_config(...).directive(name)`, which reads the file the way tinyproxy's
grammar reads it — `#` stripped, keywords case-folded, every value for a
keyword kept — and fails unless the keyword appears exactly once. That is what
closes all three: the comment and the misspelling make the keyword absent, the
contradiction makes it appear twice. The adversary's file is left alone; it is
the record of the finding.

**On the exact literals.** tinyproxy accepts `Yes`/`On`/`1` and `No`/`Off`/`0`
interchangeably, so `FilterDefaultDeny On` would mean exactly what
`FilterDefaultDeny Yes` means and these tests would still go red. That is
deliberate, and the contract asks for it: the value of a guard on a
security-critical directive is that nothing changes the line without a human
looking at it. A legitimate respelling updates the test in the same commit.

**What is not asserted here.** What tinyproxy *does* with the configuration.
tinyproxy is not installed in this sandbox (see the harness docstring), so
these tests assert what the generated file says; `h.allowlist_admits` and the
characterization suite cover what the patterns mean under those directives.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# F50 — critical. The one directive separating a sandbox from an open relay.
# ---------------------------------------------------------------------------

def test_filter_default_deny_is_yes_so_the_filter_is_an_allow_list(tmp_path):
    """Without `FilterDefaultDeny Yes` tinyproxy allows by default and the
    generated filter file becomes a deny-list: every host not named in the
    `egress_allowlist` is admitted, which is an open relay with extra steps.

    Deleting the line, commenting it out, misspelling the keyword or appending
    `FilterDefaultDeny No` all produce that proxy, and all four fail here."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterDefaultDeny") == "Yes"


# ---------------------------------------------------------------------------
# F51 — the patterns and the dialect that reads them are generated in one
# function and, until now, checked in two places that never met.
# ---------------------------------------------------------------------------

def test_filter_type_is_ere_so_the_generated_patterns_mean_what_they_say(tmp_path):
    """The generated lines are POSIX *extended* regexes — `(^|\\.)host$` uses
    grouping and alternation, and `_ere_literal` escapes exactly the set ERE
    treats as special. `FilterType regex` selects BRE, where `(`, `)` and `|`
    are ordinary characters, so the anchored subdomain group stops being a
    group and every pattern silently changes meaning."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterType") == "ere"


# ---------------------------------------------------------------------------
# F52 — DNS is case-insensitive; the patterns are not.
# ---------------------------------------------------------------------------

def test_filter_case_sensitive_is_off_so_a_capitalised_host_still_matches(tmp_path):
    """`API.Example.com` and `api.example.com` name the same host, and the
    generated pattern carries whatever case the allowlist entry was written
    in. `FilterCaseSensitive On` refuses the request rather than admitting a
    host it should not, so it fails closed — but it fails with no explanation
    and against an allowlist that names the host."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterCaseSensitive") == "Off"


# ---------------------------------------------------------------------------
# F53 — what the patterns are matched *against*.
# ---------------------------------------------------------------------------

def test_filter_urls_is_off_so_the_patterns_match_the_host_not_the_url(tmp_path):
    """`(^|\\.)example\\.com$` is anchored at both ends against a bare
    destination host. `FilterURLs On` matches it against the full URL instead,
    where the `$` anchor lands after the path — so the allowlist stops
    admitting the hosts it names."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    assert conf.directive("FilterURLs") == "Off"


# ---------------------------------------------------------------------------
# F54 — the directive names a file, and a name is only worth what is at it.
#
# This one takes two tests, and the split is not padding. `write_proxy_config`
# writes the patterns to a HOST path and names a CONTAINER path in the config
# it writes beside them; nothing inside that function can say whether the two
# are the same file. The bind mount in `ensure_proxy` is the only thing that
# makes them one, so the first test below pins the directive's value the way
# the four above pin theirs, and the second follows the value through to the
# mount. Change either the container path or the mount alone and the second
# goes red; that is the whole point of it.
# ---------------------------------------------------------------------------

def test_the_filter_directive_names_the_file_the_patterns_were_written_to(tmp_path):
    """`Filter` — tinyproxy's actual keyword, not `FilterFile`, which F54's
    text and the contract both name and which tinyproxy does not have. A
    `Filter` pointing at a path with no file at it is not an error tinyproxy
    reports; it loads no patterns, and with `FilterDefaultDeny Yes` above it
    that is a proxy which refuses everything.

    Asserted here: an absolute container path, whose basename is the name
    `write_proxy_config` gave the file it wrote the patterns to. Whether
    anything is mounted there is the next test."""
    conf = h.proxy_config(tmp_path, ["example.com"])
    named = PurePosixPath(conf.directive("Filter"))

    assert str(named) == "/etc/tinyproxy/filter"
    assert named.is_absolute()
    assert named.name == conf.filter_path.name


def test_the_bind_mount_puts_the_generated_filter_file_where_the_directive_looks(
        tmp_path, monkeypatch):
    """The half of F54 that cannot be settled in `write_proxy_config`.

    Reaches `ensure_proxy` with `_run` replaced by a recorder — argv
    inspection, no docker daemon, the idiom `test_core.py` already uses for
    `docker inspect` and `docker run`. `returncode 0` makes `image_exists`
    true; `stdout "absent"` makes `container_state` report no container, which
    is the branch that goes on to write the config and start the proxy.

    The chain is followed rather than recomputed, and each link is read out of
    the argv the executor built:

      the conf mounted at the path `Dockerfile.proxy`'s CMD loads
        -> its `Filter` directive
          -> a mount whose container side is that exact path
            -> whose host side holds the patterns for this allowlist

    Recomputing `config_dir / "filter"` and comparing it to the mount would
    prove only that two expressions in the test agree with two in the
    production code. Following the directive's own value into the mount table
    is what makes moving either end — the container path in the config, or the
    target of the `-v` — turn this red."""
    import multiagents.executor.docker as docker_mod

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "absent"      # container_state -> "absent": nothing to remove
        stderr = ""

    def fake_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return _Result()

    monkeypatch.setattr(docker_mod, "_run", fake_run)

    # A host that appears in no other fixture, so the file found at the end of
    # the chain is identifiably the one this executor generated.
    allowlist = ["mount-probe.example"]
    executor = h.make_docker_executor(tmp_path, egress_allowlist=allowlist)
    assert executor.ensure_proxy() == {"ok": True, "created": True}

    run = next(c for c in calls if c[:2] == ["docker", "run"])
    mounts = {}
    for flag, value in zip(run, run[1:]):
        if flag == "-v":
            host, container, _mode = value.rsplit(":", 2)
            mounts[container] = Path(host)

    # `Dockerfile.proxy` runs `tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf`,
    # so this is the only config the proxy reads — and therefore the only one
    # whose `Filter` directive means anything.
    assert "/etc/tinyproxy/tinyproxy.conf" in mounts, (
        f"no config mounted where tinyproxy reads one: {sorted(mounts)}")
    named = h.parse_tinyproxy_conf(mounts["/etc/tinyproxy/tinyproxy.conf"].read_text())["filter"]
    assert len(named) == 1, f"expected one Filter directive, found {named}"

    assert named[0] in mounts, (
        f"Filter names {named[0]}, but the proxy mounts nothing there: "
        f"{sorted(mounts)}")
    assert mounts[named[0]].read_text() == h.proxy_config(tmp_path, allowlist).filter_text
