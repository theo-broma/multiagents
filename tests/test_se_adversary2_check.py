"""Adversary, second pass: SE5 / Decision 16 — `multiagents docker check`.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE, SE5 and
Decision 16. Reuses the faked docker/curl harness of
`tests/test_se_sandbox_check.py` (`ensure_running` faked, curl faked behind a
modelled tinyproxy). No Docker daemon, no network.

The first test fails today (finding). The others pass today; each one kills a
mutation of `cmd_docker`'s `check` branch that the existing suite lets
survive (noted per test).
"""
from __future__ import annotations

import pytest

from test_se_sandbox_check import check, output_of  # noqa: F401  (fixture)


def test_se5_an_allowed_host_admitted_by_the_proxy_but_failing_tls_is_not_unexpected(check, capsys):
    """SE5: the check reports a refusal by the egress proxy separately from
    any other failure (DNS, TLS, HTTP), "so a correct sandbox is never
    reported UNEXPECTED". Here the proxy answered the allowed host's CONNECT
    with 200 (it was admitted) and the TLS handshake then failed — which is
    what `https://<accepted IPv4 address>/` does against nearly every server,
    since the certificate names a host, not the address. The sandbox's
    boundary is exactly right, yet today the check prints UNEXPECTED and
    exits 1."""
    result, hosts = check(["1.2.3.4"], {"1.2.3.4": "tls", "*": "proxy"})
    output = output_of(capsys)
    assert "1.2.3.4" in hosts, hosts
    assert "UNEXPECTED" not in output, output
    assert result == 0, (result, output)


def test_se5_a_denied_probe_that_gets_through_exits_nonzero(check, capsys):
    """Kills `return 0 if ok else 1` -> `return 0` and the deny-side
    `expected = not reached`: the proxy admitted the `.invalid` name."""
    result, _ = check(["a.com"], {"a.com": "ok", "*": "ok"})
    output = output_of(capsys)
    assert result == 1, (result, output)
    assert "UNEXPECTED" in output, output


@pytest.mark.parametrize("failure", ["tls", "http"])
def test_se5_a_denied_probe_past_the_filter_is_not_counted_as_refused(check, capsys, failure):
    """The denied name got past the proxy's filter (CONNECT 200) and failed
    afterwards: the filter leaked, so this is not a refusal. Kills the
    deny-side `expected = not reached`."""
    result, _ = check(["a.com"], {"a.com": "ok", "*": failure})
    output = output_of(capsys)
    assert result == 1, (result, output)
    assert "UNEXPECTED" in output, output


def test_se5_a_refused_allowed_probe_is_not_masked_by_a_good_denied_probe(check, capsys):
    """The proxy refuses the accepted host, the denied probe is refused as
    expected. Kills `ok = ok and expected` -> `ok = expected` (the last probe
    deciding the exit code)."""
    result, _ = check(["a.com"], {"*": "proxy"})
    output = output_of(capsys)
    assert result == 1, (result, output)
    assert "UNEXPECTED" in output, output


def test_se5_the_probe_uses_the_first_of_several_accepted_entries(check, capsys):
    """Kills `allowed[0]` -> `allowed[-1]`."""
    _, hosts = check(["a.com", "z.com"], {"a.com": "ok", "z.com": "ok", "*": "proxy"})
    assert "a.com" in hosts, hosts
    assert "z.com" not in hosts, hosts


def test_se5_the_denied_name_is_outside_an_allowlist_that_names_it(check, capsys):
    """An accepted entry equal to the default denied name: the denied probe
    must still target a name no accepted rule matches. Kills deleting the
    collision loop."""
    taken = "multiagents-egress-check.invalid"
    result, hosts = check([taken], {taken: "ok", "*": "proxy"})
    denied = [h for h in hosts if h != taken]
    assert denied, hosts
    assert all(h.endswith(".invalid") and h != taken
               and not h.endswith("." + taken) for h in denied), denied
    assert result == 0, (result, output_of(capsys))
