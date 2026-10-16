"""IC-C26 (407, stripping), IC-C27 (allowlist parity, destination enforcement)
and IC-C29 (revocation of live tunnels) for the egress gateway, slice 4B.1.

These are REAL handler tests: the gateway's request handler is served on a
loopback socket and driven with raw HTTP. Name resolution and dialling are
injected, controlled test-side dependencies (`ic_s4b.Egress`), so each case
observes what the handler asked the resolver, what it dialled, and what it
answered. The allowlist is the test's own configuration; no production list is
edited and no external host is contacted.

Assumed seam (NEED_INFO(egress-handler-seam)): see `ic_s4b` module docstring.
"""
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402

PW1, PW2 = "a1" * 32, "b2" * 32
ANSWERS = {"example.com": [nb.PUBLIC_V4], "api.example.com": [nb.PUBLIC_V4], "www.api.example.com": [nb.PUBLIC_V4],
           "evil.test": [nb.PUBLIC_V4], "evil-example.com": [nb.PUBLIC_V4], "notexample.com": [nb.PUBLIC_V4],
           "example.com.evil.test": [nb.PUBLIC_V4], "example.org": [nb.PUBLIC_V4]}


def make(request, allowlist=("example.com",), answers=None, **kw):
    e = nb.Egress(allowlist=allowlist, answers=ANSWERS if answers is None else answers, **kw).start()
    request.addfinalizer(e.close)
    return e


@pytest.fixture
def eg(request):
    return make(request)


def surface(status, headers, body) -> str:
    return " ".join(f"{k}: {v}" for k, v in headers.items()) + " " + body.decode("latin-1")


def no_secret(text, *secrets):
    low = text.lower()
    for secret in secrets:
        assert secret not in text, "a credential was echoed"
    assert nb.basic("ma1-att_0001", PW1).split()[1] not in text
    assert "proxy-authorization" not in low.replace("proxy-authenticate", "")


# =================================================== IC-C26: authentication (407)

def test_ic_c26_valid_credentials_forward_and_the_upstream_sees_no_proxy_authorization(eg):
    eg.upstream.body = b"UPSTREAM-OK"
    status, headers, body = eg.get("http://example.com/path?q=1")
    assert status == 200 and body == b"UPSTREAM-OK"
    (received,) = eg.upstream.requests
    assert b"proxy-authorization" not in received.lower(), received
    assert PW1.encode() not in received and nb.basic("ma1-att_0001", PW1).split()[1].encode() not in received
    assert received.startswith((b"GET /path?q=1 HTTP/1.", b"GET http://example.com/path?q=1 HTTP/1."))


def test_ic_c26_a_proxy_authorization_header_with_odd_case_or_spacing_is_still_stripped(eg):
    credential = nb.basic("ma1-att_0001", PW1)
    raw = (f"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nPROXY-authorization:   {credential}\r\n"
           f"X-Other: kept\r\n\r\n").encode()
    status, _, _ = eg.request(raw)
    assert status == 200
    received = eg.upstream.requests[0].lower()
    assert b"proxy-authorization" not in received and b"x-other: kept" in received


def test_ic_c26_missing_credentials_are_407_and_nothing_is_resolved_or_dialled(eg):
    status, headers, body = eg.get("http://example.com/", auth=False)
    assert status == 407 and eg.untouched()
    no_secret(surface(status, headers, body))


@pytest.mark.parametrize("header", [
    nb.basic("ma1-att_0001", "a1" * 31 + "a2"),            # one nibble wrong
    nb.basic("ma1-att_0001", "A1" * 32),                    # case differs
    nb.basic("ma1-att_0001", "a1" * 32 + "0"),              # longer
    nb.basic("ma1-att_0001", "a1" * 31),                    # shorter (a prefix)
    nb.basic("ma1-att_0001", ""),
    nb.basic("ma1-att_0001 ", PW1),                         # trailing space in the user
    nb.basic("MA1-ATT_0001", PW1),
    nb.basic("ma1-att_0002", PW1),                          # the other grant's user, this password
    nb.basic("ma1-att_0001", PW2),                          # this user, the other grant's password
    nb.basic("ma1-att_9999", PW1),                          # unknown user
    nb.basic("", PW1),
    nb.basic("ma1-att_0001:" + PW1, ""),                    # colon smuggled into the user
    "Basic !!!not-base64!!!",
    "Basic " + "QQ",                                        # no colon after decoding
    "Basic",
    "Basic ",
    "Bearer " + PW1,
    "Digest username=\"ma1-att_0001\"",
    PW1,
    "Basic " + nb.basic("ma1-att_0001", PW1).split()[1] + "xx",
])
def test_ic_c26_bad_credentials_are_407_without_dial_or_echo(eg, header):
    raw = f"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: {header}\r\n\r\n".encode()
    status, headers, body = eg.request(raw)
    assert status == 407, (status, header)
    assert eg.untouched(), "an upstream connection was attempted for an unauthenticated request"
    text = surface(status, headers, body)
    assert header not in text and PW1 not in text and PW2 not in text


def test_ic_c26_a_valid_credential_from_another_attempt_network_is_407(eg):
    status, headers, body = eg.get("http://example.com/", grant="g1", source="127.0.2.5")
    assert status == 407 and eg.untouched()
    no_secret(surface(status, headers, body), PW1)
    # The control: the very same request from its own network is served.
    assert eg.get("http://example.com/", grant="g1", source="127.0.1.5")[0] == 200


def test_ic_c26_the_other_grants_credential_works_only_from_its_own_network(eg):
    assert eg.get("http://example.com/", grant="g2", source="127.0.2.9")[0] == 200
    assert eg.get("http://example.com/", grant="g2", source="127.0.1.9")[0] == 407


def test_ic_c26_a_source_outside_every_attempt_network_is_407(eg):
    assert eg.get("http://example.com/", source="127.0.9.9")[0] == 407
    assert eg.untouched()


@pytest.mark.parametrize("forged", [
    ["X-Forwarded-For: 127.0.1.5"], ["Forwarded: for=127.0.1.5;by=127.0.1.1"], ["X-Real-IP: 127.0.1.5"],
    ["X-Forwarded-For: 127.0.1.5", "Forwarded: for=127.0.1.5", "X-Forwarded-Host: example.com"],
    ["X-Attempt-Network: 127.0.1.0/24", "X-MA-Grant: g1", "X-Account: other"],
    ["X-Forwarded-For: 127.0.1.5, 127.0.1.6"],
])
def test_ic_c26_forged_forwarding_or_identity_headers_prove_nothing(eg, forged):
    status, _, _ = eg.get("http://example.com/", grant="g1", source="127.0.2.5", headers=forged)
    assert status == 407 and eg.untouched()


def test_ic_c26_a_revoked_grant_gets_407_and_does_not_affect_another_grant(eg):
    assert eg.get("http://example.com/")[0] == 200
    eg.handle.revoke("g1")
    before = list(eg.dialled)
    assert eg.get("http://example.com/", grant="g1")[0] == 407
    assert eg.dialled == before, "a revoked grant caused a dial"
    assert eg.get("http://example.com/", grant="g2", source="127.0.2.5")[0] == 200


def test_ic_c26_authority_is_checked_on_each_request_of_one_connection(eg):
    c = eg.open()
    try:
        g = eg.grants["g1"]
        raw = (f"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: "
               f"{nb.basic(g['username'], g['password'])}\r\n\r\n").encode()
        c.sendall(raw)
        assert nb.read_response(c)[0] == 200
        eg.handle.revoke("g1")
        before = len(eg.upstream.requests)
        try:
            c.sendall(raw)
            status = nb.read_response(c)[0]
        except OSError:
            status = 0
        assert status in (0, 407), status
        assert len(eg.upstream.requests) == before
    finally:
        c.close()


def test_ic_c26_connect_without_credentials_is_407_and_not_dialled(eg):
    c = eg.connect_raw("example.com:443", auth=False)
    try:
        assert nb.read_response(c)[0] == 407 and eg.untouched()
    finally:
        c.close()


def test_ic_c26_connect_from_another_network_is_407(eg):
    c = eg.connect_raw("example.com:443", source="127.0.2.5")
    try:
        assert nb.read_response(c)[0] == 407 and eg.untouched()
    finally:
        c.close()


# =================================================== IC-C27: hostname parity (HTTP)

@pytest.mark.parametrize("url", ["http://example.com/", "http://api.example.com/x", "http://www.api.example.com/",
                                 "http://EXAMPLE.COM/", "http://Api.Example.Com/", "http://example.com:80/",
                                 "http://example.com/?next=http://evil.test/"])
def test_ic_c27_an_approved_domain_and_any_descendant_is_forwarded_case_insensitively(eg, url):
    status, _, body = eg.get(url)
    assert status == 200 and body == b"UPSTREAM-OK", url
    assert eg.dialled and eg.dialled[0][0] == nb.PUBLIC_V4


@pytest.mark.parametrize("url", [
    "http://evil.test/", "http://evil-example.com/", "http://notexample.com/", "http://example.com.evil.test/",
    "http://example.org/", "http://xexample.com/", "http://evil.test/example.com", "http://evil.test/?h=example.com",
    "http://evil.test/#example.com", "http://evil.test:80/example.com", "http://com/", "http://localhost/",
])
def test_ic_c27_a_hostname_outside_the_allowlist_is_403_with_no_resolution_and_no_dial(eg, url):
    status, headers, body = eg.get(url)
    assert status == 403, (url, status)
    assert eg.untouched(), (url, eg.resolved, eg.dialled)
    no_secret(surface(status, headers, body), PW1)


@pytest.mark.parametrize("url", [
    "http://example.com@evil.test/", "http://example.com:pw@evil.test/", "http://evil.test@example.com/",
    "http://example.com%40evil.test/", "http://example.com\\@evil.test/", "http://example.com%2eevil.test/",
    "http://ex ample.com/", "http://example.com\t/", "http:///", "http://:80/", "http://example.com:/",
    "http://example.com:99999/", "http://example.com:abc/", "http://[example.com]/", "http://exa\x00mple.com/",
])
def test_ic_c27_malformed_or_ambiguous_authorities_are_refused_without_dial(eg, url):
    try:
        status, _, _ = eg.get(url)
    except OSError:
        status = 0
    assert status == 400, (url, status)    # K4B.5: malformed or ambiguous authority
    assert eg.dialled == [], (url, eg.dialled)
    assert "evil.test" not in [h for h, _ in eg.resolved]


def test_ic_c27_the_request_authority_decides_not_a_conflicting_host_header(eg):
    status, _, _ = eg.get("http://example.com/", host="evil.test")
    assert "evil.test" not in [h for h, _ in eg.resolved] and eg.dialled == []
    assert status == 400    # K4B.5: a Host header that conflicts with the request authority
    eg.resolved.clear(), eg.dialled.clear()
    status, _, _ = eg.get("http://evil.test/", host="example.com")
    assert status == 400 and eg.dialled == []


def test_ic_c27_an_origin_form_request_cannot_name_a_host_through_the_host_header_alone(eg):
    g = eg.grants["g1"]
    raw = (f"GET /x HTTP/1.1\r\nHost: evil.test\r\nProxy-Authorization: {nb.basic(g['username'], g['password'])}"
           f"\r\n\r\n").encode()
    status, _, _ = eg.request(raw)
    assert status in (400, 403) and eg.dialled == []


@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "PUT", "DELETE"])
def test_ic_c27_http_methods_are_forwarded_for_approved_hosts(eg, method):
    status, _, _ = eg.get("http://example.com/m", method=method)
    assert status == 200 and eg.upstream.requests[0].startswith(method.encode() + b" ")


def test_ic_c27_the_golden_matcher_agrees_with_the_existing_allowlist_filter(request):
    """Parity (D-S4B.3): whatever the legacy `_proxy_filter` accepts for an
    approved set, the gateway accepts, and what it rejects the gateway rejects."""
    from multiagents.executor.docker import DockerExecutor
    allow = ["example.com", "sub.other.org", "93.184.216.35"]
    names = ["example.com", "A.Example.COM", "x.y.example.com", "evil-example.com", "example.com.evil.test",
             "sub.other.org", "other.org", "z.sub.other.org", "93.184.216.35", "x.93.184.216.35", "93.184.216.350"]
    answers = {n.lower(): [nb.PUBLIC_V4] for n in names}
    e = make(request, allowlist=allow, answers=answers)
    legacy = DockerExecutor.__new__(DockerExecutor)
    legacy.config = {"egress_allowlist": allow}
    import re
    patterns = [line for line in legacy._proxy_filter().splitlines() if line]
    for name in names:
        expected = any(re.search(p, name, re.I) for p in patterns)
        status = e.get(f"http://{name}/")[0]
        assert (status == 200) == expected, (name, status, expected)


def test_ic_c27_an_approved_canonical_ipv4_entry_is_exact(request):
    e = make(request, allowlist=("93.184.216.35",))
    assert e.get("http://93.184.216.35/")[0] == 200 and e.dialled == [("93.184.216.35", 80)]
    e.dialled.clear()
    for other in ("93.184.216.34", "93.184.216.36", "93.184.216.3", "93.184.216.35.example.com"):
        assert e.get(f"http://{other}/")[0] == 403, other
    assert e.dialled == []


@pytest.mark.parametrize("literal", ["1572395042", "0x5db8d822", "0135.0270.0330.0042", "93.184.55330", "93.0xb8.216.34"])
def test_ic_c27_non_canonical_ip_spellings_are_never_dialled(request, literal):
    e = make(request, allowlist=("93.184.216.34",))
    status = e.get(f"http://{literal}/")[0]
    assert status in (400, 403) and e.dialled == [], literal


@pytest.mark.parametrize("literal", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "192.168.0.1", "172.16.0.9", "100.64.0.1",
                                     "0.0.0.0", "224.0.0.1", "255.255.255.255"])
def test_ic_c27_the_address_prohibition_applies_even_to_an_approved_literal_ip(request, literal):
    e = make(request, allowlist=(literal,))
    status, _, _ = e.get(f"http://{literal}/")
    assert status == 403 and e.dialled == [], literal
    c = e.connect_raw(f"{literal}:443")
    try:
        assert nb.read_response(c)[0] == 403 and e.dialled == []
    finally:
        c.close()


@pytest.mark.parametrize("literal", ["[::1]", "[fe80::1]", "[2606:4700:4700::1111]", "[::ffff:127.0.0.1]"])
def test_ic_c27_ipv6_literals_are_not_approved_and_never_dialled(eg, literal):
    status, _, _ = eg.get(f"http://{literal}/")
    assert status in (400, 403) and eg.dialled == []


# ======================================= IC-C27: the resolved destination

FORBIDDEN = ["10.0.0.1", "10.255.255.255", "127.0.0.1", "127.1.2.3", "169.254.169.254", "172.16.5.4", "172.31.0.1", "192.168.1.1",
             "100.64.0.1", "224.0.0.1", "239.255.255.250", "0.0.0.0", "255.255.255.255", "240.0.0.1",
             "::1", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "::", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
             "::ffff:169.254.169.254", "::ffff:192.168.0.1"]


@pytest.mark.parametrize("address", FORBIDDEN)
def test_ic_c27_a_hostname_resolving_to_a_non_global_address_is_403_and_not_dialled(request, address):
    e = make(request, answers={"example.com": [address]})
    status, headers, body = e.get("http://example.com/")
    assert status == 403 and e.dialled == [], address
    no_secret(surface(status, headers, body), PW1)


@pytest.mark.parametrize("address", FORBIDDEN)
def test_ic_c27_the_same_prohibition_holds_for_connect(request, address):
    e = make(request, answers={"example.com": [address]})
    c = e.connect_raw("example.com:443")
    try:
        assert nb.read_response(c)[0] == 403 and e.dialled == []
    finally:
        c.close()


@pytest.mark.parametrize("answers", [
    [nb.PUBLIC_V4, "10.0.0.1"], ["10.0.0.1", nb.PUBLIC_V4], [nb.PUBLIC_V4, "127.0.0.1", nb.PUBLIC_V4_B],
    [nb.PUBLIC_V6, "fe80::1"], [nb.PUBLIC_V4, "::1"], [nb.PUBLIC_V4, "169.254.169.254"],
    [nb.PUBLIC_V4, "::ffff:10.0.0.1"], [nb.PUBLIC_V4, nb.PUBLIC_V4_B, "192.168.0.1"],
])
def test_ic_c27_a_mixed_answer_set_is_refused_whole(request, answers):
    e = make(request, answers={"example.com": answers})
    assert e.get("http://example.com/")[0] == 403 and e.dialled == [], answers
    c = e.connect_raw("example.com:443")
    try:
        assert nb.read_response(c)[0] == 403 and e.dialled == []
    finally:
        c.close()


def test_ic_c27_all_global_answers_are_accepted_and_only_a_checked_numeric_address_is_dialled(request):
    answers = [nb.PUBLIC_V4, nb.PUBLIC_V4_B, nb.PUBLIC_V6]
    e = make(request, answers={"example.com": answers})
    assert e.get("http://example.com/")[0] == 200
    assert len(e.dialled) == 1 and e.dialled[0][0] in answers and e.dialled[0][1] == 80
    assert len(e.resolved) == 1 and e.resolved[0][0].lower() == "example.com", "one resolution per request, no second unchecked lookup"


def test_ic_c27_a_gateway_management_host_or_attempt_network_address_is_refused_even_if_globally_numbered(request):
    e = make(request, answers={"example.com": ["93.184.216.34"], "api.example.com": ["93.184.217.9"]},
             forbidden=["93.184.216.0/24"])
    assert e.get("http://example.com/")[0] == 403 and e.dialled == []
    assert e.get("http://api.example.com/")[0] == 200 and e.dialled == [("93.184.217.9", 80)]
    # Also as a literal, and inside a mixed set.
    e2 = make(request, allowlist=("93.184.216.34",), forbidden=["93.184.216.0/24"])
    assert e2.get("http://93.184.216.34/")[0] == 403 and e2.dialled == []


def test_ic_c27_every_new_connection_resolves_and_checks_again_rebinding(request):
    e = make(request)
    e.sequence["example.com"] = [[nb.PUBLIC_V4], ["127.0.0.1"]]
    assert e.get("http://example.com/")[0] == 200
    assert e.get("http://example.com/")[0] == 403
    assert e.dialled == [(nb.PUBLIC_V4, 80)], "the rebound address was dialled"
    assert len(e.resolved) == 2


def test_ic_c27_rebinding_in_the_other_direction_recovers(request):
    e = make(request)
    e.sequence["example.com"] = [["10.0.0.9"], [nb.PUBLIC_V4]]
    assert e.get("http://example.com/")[0] == 403 and e.dialled == []
    assert e.get("http://example.com/")[0] == 200


def test_ic_c27_no_answer_is_a_scrubbed_502_not_a_403_and_not_a_retry(request):
    e = make(request, answers={"example.com": []})
    status, headers, body = e.get("http://example.com/")
    assert status == 502 and e.dialled == []
    no_secret(surface(status, headers, body), PW1)


def test_ic_c27_a_resolver_failure_is_a_scrubbed_502(request):
    e = make(request)
    e.resolve_error = OSError("SECRET-DNS-DETAIL resolver at 10.9.9.9 failed")
    status, headers, body = e.get("http://example.com/")
    text = surface(status, headers, body)
    assert status == 502 and e.dialled == []
    assert "SECRET-DNS-DETAIL" not in text and "10.9.9.9" not in text and "Traceback" not in text
    no_secret(text, PW1)


def test_ic_c27_a_dial_failure_is_a_scrubbed_502_with_no_second_dial_or_direct_retry(request):
    e = make(request)
    e.dial_error = ConnectionRefusedError(f"SECRET-DIAL-DETAIL {nb.PUBLIC_V4}")
    status, headers, body = e.get("http://example.com/")
    text = surface(status, headers, body)
    assert status == 502
    assert len(e.dialled) == 1 and len(e.resolved) == 1, (e.dialled, e.resolved)
    assert "SECRET-DIAL-DETAIL" not in text and "Traceback" not in text
    no_secret(text, PW1)


def test_ic_c27_a_gateway_followed_redirect_is_not_trusted_to_reach_new_destinations(request):
    for location in ("http://evil.test/x", "http://10.0.0.1/", "http://127.0.0.1:8888/"):
        up = nb.Upstream(status="302 Found", extra=f"Location: {location}\r\n")
        e = make(request, upstream=up)
        status, headers, _ = e.get("http://example.com/")
        assert status in (200, 301, 302, 303, 307, 308, 403, 502)
        assert "evil.test" not in [h for h, _ in e.resolved], location
        assert all(a == nb.PUBLIC_V4 for a, _ in e.dialled), (location, e.dialled)


# ======================================================= IC-C27: CONNECT forms

def tunnel(e, target, **kw):
    c = e.connect_raw(target, **kw)
    status, headers, body = nb.read_response(c)
    return c, status


@pytest.mark.parametrize("target", ["example.com:443", "api.example.com:443", "EXAMPLE.com:443", "www.api.example.com:443"])
def test_ic_c27_connect_to_443_of_an_approved_host_opens_a_tunnel_that_carries_exactly_the_payload(eg, target):
    c, status = tunnel(eg, target)
    try:
        assert status == 200
        assert nb.tunnel_alive(c, b"payload-bytes"), "the tunnel must relay exactly what is sent"
        assert eg.dialled == [(nb.PUBLIC_V4, 443)]
    finally:
        c.close()


@pytest.mark.parametrize("port", ["80", "8443", "22", "444", "4430", "44"])
def test_ic_c27_connect_is_refused_on_any_port_but_443(eg, port):
    c, status = tunnel(eg, f"example.com:{port}")
    try:
        assert status == 403 and eg.dialled == [], (port, status)    # K4B.5: policy
    finally:
        c.close()


@pytest.mark.parametrize("port", ["65536", "-443", "+443", "443.0", "443 "])
def test_ic_c27_connect_with_a_malformed_port_is_400(eg, port):
    c, status = tunnel(eg, f"example.com:{port}")
    try:
        assert status == 400 and eg.dialled == [], (port, status)    # K4B.5: malformed authority
    finally:
        c.close()


def test_ic_c27_connect_to_port_zero_is_refused_without_dial(eg):
    c, status = tunnel(eg, "example.com:0")      # K4B.5 does not say whether 0 is malformed or policy
    try:
        assert status in (400, 403) and eg.dialled == []
    finally:
        c.close()


@pytest.mark.parametrize("target", ["evil.test:443", "evil-example.com:443", "example.com.evil.test:443", "notexample.com:443",
                                    "com:443", "localhost:443"])
def test_ic_c27_connect_to_a_host_outside_the_allowlist_is_403_without_dial(eg, target):
    c, status = tunnel(eg, target)
    try:
        assert status == 403 and eg.untouched(), target
    finally:
        c.close()


@pytest.mark.parametrize("target", ["example.com", "example.com:", ":443", "http://example.com:443", "example.com:443/x",
                                    "user@example.com:443", "example.com:443:443", "[example.com]:443",
                                    "example.com:443?x", "example.com.:443x"])
def test_ic_c27_malformed_tunnel_forms_are_400(eg, target):
    c, status = tunnel(eg, target)
    try:
        assert status == 400 and eg.dialled == [], (target, status)    # K4B.5
    finally:
        c.close()


@pytest.mark.parametrize("target", ["[::1]:443", "*.example.com:443"])
def test_ic_c27_well_formed_but_unsupported_tunnel_targets_are_refused(eg, target):
    c, status = tunnel(eg, target)      # K4B.5 leaves 400 vs 403 open for these
    try:
        assert status in (400, 403) and eg.dialled == [], (target, status)
    finally:
        c.close()


def test_ic_c27_connect_ignores_a_conflicting_host_header(eg):
    c = eg.connect_raw("evil.test:443", headers=["Host: example.com:443"])
    try:
        assert nb.read_response(c)[0] in (400, 403) and eg.dialled == []    # outside the allowlist, and conflicting
    finally:
        c.close()
    c = eg.connect_raw("example.com:443", headers=["Host: evil.test:443"])
    try:
        assert nb.read_response(c)[0] == 400    # K4B.5: conflicting Host header
    finally:
        c.close()
    assert "evil.test" not in [h for h, _ in eg.resolved] and eg.dialled == []


def test_ic_c27_non_http_junk_and_oversized_requests_are_refused_without_dial(eg):
    for raw in (b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\x00" * 64, b"GET\r\n\r\n", b"CONNECT\r\n\r\n",
                b"GET http://example.com/ HTTP/1.1\r\n" + b"X: " + b"a" * 70000 + b"\r\n\r\n"):
        try:
            status, _, _ = eg.request(raw)
        except OSError:
            status = 0
        assert status in (0, 400, 407, 413, 431), status
    assert eg.dialled == []


# ============================== IC-C29: revocation closes accepted tunnels

def test_ic_c29_revocation_closes_an_accepted_tunnel_and_stops_forwarding(eg):
    mine, status = tunnel(eg, "example.com:443", grant="g1", source="127.0.1.5")
    other, status2 = tunnel(eg, "example.com:443", grant="g2", source="127.0.2.5")
    try:
        assert status == 200 and status2 == 200
        assert nb.tunnel_alive(mine) and nb.tunnel_alive(other)
        eg.handle.revoke("g1")
        deadline = time.monotonic() + nb.SHORT
        alive = True
        while alive and time.monotonic() < deadline:
            alive = nb.tunnel_alive(mine, b"x")
        assert not alive, "a tunnel accepted before revocation still forwards after it"
        assert nb.tunnel_alive(other), "revoking one attempt's grant must not touch another attempt's tunnel"
    finally:
        mine.close(), other.close()


def test_ic_c29_revocation_closes_the_gateways_upstream_side_too(eg):
    c, status = tunnel(eg, "example.com:443")
    try:
        assert status == 200 and nb.tunnel_alive(c)
        eg.upstream.closed.clear()
        eg.handle.revoke("g1")
        assert eg.upstream.closed.wait(nb.SHORT), "the upstream connection was left open after revocation"
    finally:
        c.close()


def test_ic_c29_revoking_every_grant_leaves_no_request_able_to_dial(eg):
    eg.handle.revoke("g1")
    eg.handle.revoke("g2")
    assert eg.get("http://example.com/", grant="g1")[0] == 407
    assert eg.get("http://example.com/", grant="g2", source="127.0.2.5")[0] == 407
    assert eg.dialled == []
    eg.handle.revoke("g1")     # repeating is harmless


def test_ic_c29_revoking_an_unknown_grant_does_not_disturb_others(eg):
    try:
        eg.handle.revoke("no-such-grant")
    except KeyError:
        pass
    assert eg.get("http://example.com/")[0] == 200


def test_ic_c29_many_simultaneous_tunnels_all_close_on_revocation(eg):
    tunnels = [tunnel(eg, "example.com:443")[0] for _ in range(8)]
    try:
        assert all(nb.tunnel_alive(t) for t in tunnels)
        eg.handle.revoke("g1")
        deadline = time.monotonic() + nb.SHORT
        while time.monotonic() < deadline and any(nb.tunnel_alive(t, b"x") for t in tunnels):
            pass
        assert not any(nb.tunnel_alive(t, b"x") for t in tunnels)
    finally:
        for t in tunnels:
            t.close()
