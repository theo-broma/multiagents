"""IC-C28 / K4B2.2 / K4B2.5 / K4B2.6 (slice 4B.2): the auth gateway's REAL
request handler, served on loopback and driven by raw sockets. The upstream is
the synthetic adapter behind the MULTIAGENTS_TEST_GATEWAY_BACKEND seam
(`ic_s4b2_backend`); the "source network" of a request is the loopback address
the client binds, as in the egress handler tests.

Contract: context/specs/phase7-part2-contract.md, "IC-C28", "Slice 4B.2
decisions". The observable for "no account selection and no upstream open
before authentication" is the adapter boundary: nothing may be opened.
Containment is NOT claimed here; see test_k4b2_auth_docker.py.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402


@pytest.fixture
def auth(tmp_path, monkeypatch):
    a = ab.Auth(tmp_path, monkeypatch).start()
    yield a
    a.close()


def bearer(a, grant="g1"):
    return a.grants[grant]["bearer"]


def no_secret_in(a, status_headers_body, *extra):
    status, headers, body = status_headers_body
    text = (str(headers) + body.decode("latin-1"))
    for secret in (bearer(a, "g1"), bearer(a, "g2"), *extra):
        if secret:
            assert secret not in text, "a credential was echoed in the response"


# ------------------------------------------------------- the test upstream itself

def test_k4b2_5_the_synthetic_response_read_blocks_until_fed_and_close_unblocks_it():
    response = be.Response(stream=True)
    got = []
    t = threading.Thread(target=lambda: got.append(response.read(10)), daemon=True)
    t.start()
    t.join(0.3)
    assert t.is_alive(), "the test response must block a read until it is fed"
    response.feed(b"abc")
    t.join(be.GATE)
    assert got == [b"abc"]
    t2 = threading.Thread(target=lambda: got.append(response.read(10)), daemon=True)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()
    response.close()
    t2.join(be.GATE)
    assert not t2.is_alive() and got[-1] == b"", "close must unblock a pending read"


# ------------------------------------------------------------------ the happy path

def test_ic_c28_a_valid_bearer_from_its_subnet_reaches_the_upstream(auth):
    status, headers, body = auth.send("POST", "/v1/messages?beta=true", body=b'{"hello":"fiction"}')
    assert status == 200 and be.MARKER in body
    (opened,) = auth.adapter.opened
    assert (opened["method"], opened["path"], opened["body"]) == ("POST", "/v1/messages?beta=true", b'{"hello":"fiction"}')


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_k4b2_2_get_head_and_post_are_served(auth, method):
    status, headers, body = auth.send(method, "/v1/models")
    assert status == 200
    assert [o["method"] for o in auth.adapter.opened] == [method]
    if method == "HEAD":
        assert body == b"", "a HEAD response carries no body"
    else:
        assert be.MARKER in body


def test_ic_c28_the_guest_bearer_is_not_forwarded_to_the_upstream(auth):
    auth.send("POST", "/v1/messages", headers=["x-api-key: " + bearer(auth), "X-Note: hello"])
    (opened,) = auth.adapter.opened
    flat = " ".join(f"{k}: {v}" for k, v in opened["headers"].items())
    assert "ma-auth-v1" not in flat, flat
    assert opened["headers"].get("X-Note", opened["headers"].get("x-note")) == "hello", "ordinary headers pass"


def test_k4b2_7_provider_and_run_come_from_the_grant_not_from_request_fields(auth):
    forged = ["X-Provider: other", "X-Run-Id: run_0002", "X-Multiagents-Agent: someone", "Provider: other",
              "X-Forwarded-For: 127.0.2.9", "Forwarded: for=127.0.2.9", "X-Real-IP: 127.0.2.9"]
    auth.send("POST", "/v1/messages", grant="g1", headers=forged, source="127.0.1.5")
    auth.send("POST", "/v1/messages", grant="g2", headers=["X-Provider: acme", "X-Run-Id: run_0001"], source="127.0.2.5")
    a, b = auth.adapter.opened
    assert (a["provider"], a["run_id"]) == ("acme", "run_0001")
    assert (b["provider"], b["run_id"]) == ("other", "run_0002")


# ----------------------------------------------------- 401 before anything is opened

def legacy_tokens():
    from multiagents import authproxy
    return {"legacy-mxa": authproxy.mint_token("agent-fiction", "fictional-secret"),
            "legacy-mxa2": authproxy.mint_token("agent-fiction", "fictional-secret", "acme")}


def bad_credentials(a):
    real = bearer(a, "g1")
    prefix = "ma-auth-v1."
    tokens = legacy_tokens()
    cases = {
        "missing": dict(authorization=""),
        "empty-bearer": dict(authorization="Bearer "),
        "bare-token-no-scheme": dict(authorization=real),
        "basic-scheme": dict(authorization=ab.basic("ma1-att_0001", real)),
        "forged": dict(token=prefix + "c" * 64),
        "wrong-version": dict(token="ma-auth-v2." + real.removeprefix(prefix)),
        "no-prefix": dict(token=real.removeprefix(prefix)),
        "truncated": dict(token=real[:-1]),
        "extended": dict(token=real + "a"),
        "uppercase": dict(token=prefix + real.removeprefix(prefix).upper().replace("A", "A")),
        "trailing-space": dict(authorization="Bearer " + real + " x"),
        "two-tokens": dict(authorization="Bearer " + real + " " + real),
        "egress-password": dict(token="a1" * 32),
        "egress-basic": dict(authorization=ab.basic("ma1-att_0001", "a1" * 32)),
        "other-grants-bearer": dict(token=bearer(a, "g2")),
        "legacy-mxa": dict(token=tokens["legacy-mxa"]),
        "legacy-mxa2": dict(token=tokens["legacy-mxa2"]),
        "legacy-mxa-bare": dict(authorization=tokens["legacy-mxa"]),
        "legacy-mxa2-bare": dict(authorization=tokens["legacy-mxa2"]),
    }
    return cases


@pytest.mark.parametrize("name", ["missing", "empty-bearer", "bare-token-no-scheme", "basic-scheme", "forged",
                                  "wrong-version", "no-prefix", "truncated", "extended", "uppercase",
                                  "trailing-space", "two-tokens", "egress-password", "egress-basic",
                                  "other-grants-bearer", "legacy-mxa", "legacy-mxa2", "legacy-mxa-bare",
                                  "legacy-mxa2-bare"])
def test_ic_c28_every_bad_credential_is_401_before_any_account_or_upstream(auth, name):
    kwargs = bad_credentials(auth)[name]
    if name == "uppercase":
        kwargs = dict(token="ma-auth-v1." + bearer(auth).removeprefix("ma-auth-v1.").upper())
        # the grant's value is lowercase hex, so an uppercase spelling differs only if it has letters
        if kwargs["token"] == bearer(auth):
            pytest.skip("the fixture bearer has no letters")
    got = auth.send("POST", "/v1/messages", **kwargs)
    assert got[0] == 401, got
    assert auth.untouched(), "an account or upstream was selected before authentication"
    no_secret_in(auth, got, kwargs.get("token"), kwargs.get("authorization"))


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_ic_c28_unauthenticated_requests_are_401_for_every_served_method(auth, method):
    got = auth.send(method, "/v1/models", authorization="")
    assert got[0] == 401 and auth.untouched()


def test_ic_c28_the_valid_bearer_in_another_header_is_not_authority(auth):
    for header in ("x-api-key", "Proxy-Authorization", "X-Authorization", "Cookie"):
        value = bearer(auth) if header in ("x-api-key", "X-Authorization", "Cookie") else "Bearer " + bearer(auth)
        got = auth.send("POST", "/v1/messages", authorization="", headers=[f"{header}: {value}"])
        assert got[0] == 401, (header, got[0])
    assert auth.untouched()


def test_ic_c28_the_bearer_is_accepted_only_from_its_grants_subnets(auth):
    ok = auth.send("POST", "/v1/messages", source="127.0.1.77")
    assert ok[0] == 200 and len(auth.adapter.opened) == 1
    for source in ("127.0.2.5", "127.0.9.5", "127.0.0.1"):
        got = auth.send("POST", "/v1/messages", source=source)
        assert got[0] == 401, (source, got[0])
    assert len(auth.adapter.opened) == 1, "a wrong-subnet request reached the upstream"


def test_ic_c28_forged_forwarding_fields_do_not_move_a_request_into_a_grants_network(auth):
    forged = ["X-Forwarded-For: 127.0.1.5", "Forwarded: for=127.0.1.5;by=127.0.1.5", "X-Real-IP: 127.0.1.5",
              "X-Forwarded-Host: ma-auth", "Via: 1.1 127.0.1.5"]
    got = auth.send("POST", "/v1/messages", source="127.0.2.5", headers=forged, host="127.0.1.5")
    assert got[0] == 401 and auth.untouched()


def test_ic_c28_a_bad_bearer_is_401_even_when_the_request_target_is_also_bad(auth):
    for target in ("@attacker.invalid/x", ".attacker.invalid/x", ":8443/x"):
        got = auth.send("POST", target, token="ma-auth-v1." + "d" * 64)
        assert got[0] == 401, (target, got[0])
    assert auth.untouched()


def test_ic_c28_401_responses_are_not_distinguishable_by_status_across_causes(auth):
    auth.handle.revoke("g2")
    statuses = {auth.send("POST", "/v1/messages", token=bearer(auth, "g2"), source="127.0.2.5")[0],
                auth.send("POST", "/v1/messages", token="ma-auth-v1." + "e" * 64, source="127.0.2.5")[0],
                auth.send("POST", "/v1/messages", source="127.0.9.5")[0]}
    assert statuses == {401}


# ---------------------------------------------------- the fixed upstream, safe targets

@pytest.mark.parametrize("target", ["@attacker.invalid/x", ".attacker.invalid/x", ":8443/x",
                                    "http://attacker.invalid/x", "https://api.anthropic.com@attacker.invalid/x",
                                    "attacker.invalid/x", "x@attacker.invalid"])
def test_ic_c28_a_target_that_would_leave_the_fixed_upstream_is_refused_without_an_open(auth, target):
    got = auth.send("POST", target)
    assert got[0] == 400, (target, got[0])
    assert auth.untouched()


def test_ic_c28_the_adapter_only_ever_sees_origin_form_paths(auth):
    for target in ("/v1/messages", "/v1/messages/count_tokens?x=1", "/v1/models/claude-fiction"):
        assert auth.send("POST", target)[0] == 200
    assert [o["path"] for o in auth.adapter.opened] == ["/v1/messages", "/v1/messages/count_tokens?x=1",
                                                       "/v1/models/claude-fiction"]
    assert all(o["path"].startswith("/") for o in auth.adapter.opened)


# ---------------------------------------------------------------------- methods

@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_k4b2_2_other_methods_are_405_with_an_allow_list_and_no_open(auth, method):
    got = auth.send(method, "/v1/messages")
    assert got[0] == 405, (method, got[0])
    allowed = {m.strip() for m in got[1].get("allow", "").split(",")}
    assert allowed == {"GET", "HEAD", "POST"}, got[1]
    assert auth.untouched()


def test_k4b2_2_connect_is_never_a_tunnel(auth):
    raw = (f"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n"
           f"Authorization: Bearer {bearer(auth)}\r\nProxy-Authorization: Bearer {bearer(auth)}\r\n\r\n").encode()
    status, headers, body = auth.raw(raw)
    assert not 200 <= status < 300, status
    assert auth.untouched()


# --------------------------------------------------------------- request framing

def test_ic_c28_a_chunked_request_body_is_411_and_not_opened(auth):
    raw = (f"POST /v1/messages HTTP/1.1\r\nHost: ma-auth:8930\r\nAuthorization: Bearer {bearer(auth)}\r\n"
           "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode()
    assert auth.raw(raw)[0] == 411 and auth.untouched()


@pytest.mark.parametrize("length", ["abc", "-1", "1, 2", "1e3"])
def test_ic_c28_an_invalid_or_conflicting_content_length_is_400_and_not_opened(auth, length):
    raw = (f"POST /v1/messages HTTP/1.1\r\nHost: ma-auth:8930\r\nAuthorization: Bearer {bearer(auth)}\r\n"
           f"Content-Length: {length}\r\n\r\nxx").encode()
    assert auth.raw(raw)[0] == 400 and auth.untouched()


def test_ic_c28_a_body_over_the_64_mib_bound_is_413_without_being_read(auth):
    raw = (f"POST /v1/messages HTTP/1.1\r\nHost: ma-auth:8930\r\nAuthorization: Bearer {bearer(auth)}\r\n"
           f"Content-Length: {64 * 1024 * 1024 + 1}\r\n\r\n").encode()
    assert auth.raw(raw)[0] == 413 and auth.untouched()


# --------------------------------------------------------------- upstream outcomes

def test_ic_c28_an_upstream_failure_is_a_scrubbed_502(auth):
    auth.adapter.error = OSError("detail-with-internal-address 10.9.8.7 and a-token-value")
    got = auth.send("POST", "/v1/messages")
    assert got[0] == 502
    text = got[2].decode("latin-1")
    assert "10.9.8.7" not in text and "a-token-value" not in text and bearer(auth) not in text


def test_ic_c28_an_upstream_error_status_reaches_the_guest(auth):
    auth.adapter.next = [be.Response(404, body=b'{"type":"error","error":{"type":"not_found_error","message":"fiction"}}')]
    got = auth.send("POST", "/v1/messages")
    assert got[0] == 404 and b"not_found_error" in got[2]


def test_ic_c28_no_log_output_holds_a_bearer(auth, capfd):
    auth.send("POST", "/v1/messages")
    auth.send("POST", "/v1/messages", token="ma-auth-v1." + "f" * 64)
    auth.send("POST", "/v1/messages", authorization="")
    out, err = capfd.readouterr()
    for secret in (bearer(auth), bearer(auth, "g2"), "ma-auth-v1." + "f" * 64):
        assert secret not in out + err, "a bearer reached the gateway's output"


# ------------------------------------------------------------------- revocation

def test_ic_c29_a_revoked_bearer_is_401_and_other_grants_are_untouched(auth):
    assert auth.send("POST", "/v1/messages")[0] == 200
    auth.handle.revoke("g1")
    auth.handle.revoke("g1")                       # repeating a revoke is harmless
    opened = len(auth.adapter.opened)
    assert auth.send("POST", "/v1/messages")[0] == 401
    assert len(auth.adapter.opened) == opened, "a revoked grant reached the upstream"
    assert auth.send("POST", "/v1/messages", grant="g2", source="127.0.2.5")[0] == 200


def stream_client(a, grant, source):
    a.adapter.next.append(be.Response(200, {"content-type": "text/event-stream"}, stream=True))
    return a.stream(grant=grant, source=source)


def test_k4b2_6_revoking_a_grant_cuts_its_streaming_response_and_closes_the_upstream_read(auth):
    c = stream_client(auth, "g1", "127.0.1.5")
    try:
        assert auth.adapter.wait_opened(1)
        upstream = auth.adapter.responses[0]
        upstream.feed(b"data: one\n\n")
        assert b"data: one" in ab.read_until(c, b"data: one"), "the stream did not start"
        auth.handle.revoke("g1")
        assert ab.closes(c), "the guest's stream was not cut by the revocation"
        assert upstream.closed.wait(be.GATE), "the in-flight upstream read was not closed"
        upstream.feed(b"data: late\n\n")                  # nothing more may be forwarded
        assert b"late" not in ab.read_until(c, b"late", timeout=0.5)
    finally:
        c.close()


def test_k4b2_6_revoking_cuts_a_stream_that_has_sent_nothing_yet(auth):
    c = stream_client(auth, "g1", "127.0.1.5")
    try:
        assert auth.adapter.wait_opened(1)
        auth.handle.revoke("g1")
        assert ab.closes(c)
        assert auth.adapter.responses[0].closed.wait(be.GATE), "a pending upstream read survived the revocation"
    finally:
        c.close()


def test_k4b2_6_revoking_one_grant_leaves_another_grants_stream_alive(auth):
    one = stream_client(auth, "g1", "127.0.1.5")
    assert auth.adapter.wait_opened(1)
    two = stream_client(auth, "g2", "127.0.2.5")
    try:
        assert auth.adapter.wait_opened(2)
        first, second = auth.adapter.responses
        auth.handle.revoke("g1")
        assert ab.closes(one) and first.closed.wait(be.GATE)
        assert not second.closed.is_set(), "revoking one grant closed another grant's upstream"
        second.feed(b"data: two\n\n")
        assert b"data: two" in ab.read_until(two, b"data: two"), "the other grant's stream was cut"
    finally:
        one.close()
        two.close()


def test_k4b2_6_a_stream_accepted_before_the_revoke_is_not_a_window_for_new_requests(auth):
    c = stream_client(auth, "g1", "127.0.1.5")
    try:
        assert auth.adapter.wait_opened(1)
        auth.handle.revoke("g1")
        before = len(auth.adapter.opened)
        assert auth.send("POST", "/v1/messages")[0] == 401
        assert len(auth.adapter.opened) == before
    finally:
        c.close()


# ------------------------------------------------ the verifier seam (K4B2.8)

def test_k4b2_8_the_gateway_authenticates_by_the_sha256_verifier_of_the_full_bearer(auth):
    status, _, body = auth.send("POST", "/v1/messages")
    assert status == 200 and be.MARKER in body
    assert len(auth.adapter.opened) == 1


def test_k4b2_8_the_verifier_itself_is_not_a_bearer(auth):
    verifier = ab.verifier_of(bearer(auth))
    for kwargs in (dict(token=verifier), dict(token="ma-auth-v1." + verifier), dict(token=verifier.upper())):
        assert auth.send("POST", "/v1/messages", **kwargs)[0] == 401, kwargs
    assert auth.untouched()


def test_k4b2_8_only_the_full_bearer_string_matches_its_verifier(auth):
    full = bearer(auth)
    for part in (full.removeprefix("ma-auth-v1."), full[:-1], full + "0", full.upper()):
        assert auth.send("POST", "/v1/messages", token=part)[0] == 401, part
    assert auth.untouched()


def test_k4b2_8_a_plaintext_in_the_verifier_field_is_not_a_bearer(tmp_path, monkeypatch):
    token = "ma-auth-v1." + "a1" * 32
    wrong = {"g1": {"verifier": token, "networks": ["127.0.1.0/24"], "provider": "acme", "run_id": "run_0001"}}
    a = ab.Auth(tmp_path, monkeypatch, grants={"g1": dict(wrong["g1"], bearer=token)})
    try:
        handle = nb.have("serve_auth")(listen=("127.0.0.1", 0), grants=wrong, backend=a.adapter)
        try:
            c = socket_to(handle)
            c.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: ma-auth:8930\r\nAuthorization: Bearer {token}\r\n"
                       "Content-Length: 2\r\n\r\n{}").encode())
            assert ab.response(c)[0] == 401, "a plaintext was accepted as a verifier"
            assert a.untouched()
        finally:
            handle.close()
    except Exception as exc:
        if "401" in str(exc) or isinstance(exc, AssertionError):
            raise
        # refusing a malformed verifier at construction is equally acceptable
    finally:
        a.close()


def socket_to(handle):
    import socket
    c = socket.socket()
    c.settimeout(ab.SHORT)
    c.bind(("127.0.1.5", 0))
    c.connect(tuple(handle.address))
    return c


# --------------------------------------------- credentials are not interchangeable

def test_k4b2_3_an_auth_bearer_presented_at_the_egress_gateway_is_407_without_dial(tmp_path, monkeypatch):
    eg = nb.Egress(allowlist=["example.com"], answers={"example.com": [nb.PUBLIC_V4]}).start()
    token = "ma-auth-v1." + "a1" * 32
    try:
        for header in (ab.basic("ma1-att_0001", token), "Bearer " + token, ab.basic("ma-auth", token),
                       ab.basic("ma1-att_0001", "Bearer " + token)):
            raw = ("GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: " + header
                   + "\r\n\r\n").encode()
            status, headers, body = eg.request(raw)
            assert status == 407, (header[:20], status)
            assert token not in str(headers) + body.decode("latin-1")
        status, _, _ = eg.request(("GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nAuthorization: Bearer "
                                   + token + "\r\n\r\n").encode())
        assert status == 407
        assert eg.untouched(), "the egress gateway resolved or dialled for an auth bearer"
    finally:
        eg.close()


def test_k4b2_3_an_egress_password_presented_at_the_auth_gateway_is_401_without_open(auth):
    for kwargs in (dict(token="a1" * 32), dict(authorization=ab.basic("ma1-att_0001", "a1" * 32)),
                   dict(authorization="Bearer ma1-att_0001:" + "a1" * 32)):
        assert auth.send("POST", "/v1/messages", **kwargs)[0] == 401
    assert auth.untouched()
