"""AP1 — F21 and part 1's Decision 4 leaks: account identity never reaches an agent.

Contract: `context/specs/findings-c1-c2-part2.md`, Part 2b, AP1 (and
Decision 7). The surface is `authproxy._scrub(payload: bytes) -> bytes`,
which the contract names, and the HTTP error path that uses it. "Unchanged"
for a JSON body means the same parsed value (HF1 re-serialises it), so every
JSON comparison here is on `json.loads` of the result.

`redact.scrub` keeps its current behaviour for its other callers: those
tests are green today and must stay green.
"""

import json
import sys
from pathlib import Path

import pytest

from multiagents import authproxy, redact

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap

MASK = redact.MASK
LITERAL = "ap-one-registered-sensitive-value"
LITERAL_2 = "ap-one-second-registered-value"

ORG_KEYS = ["organization", "organization_id", "organization_name", "org",
            "org_id", "org_name", "email", "user_email"]


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    # Fixture isolation only, as in test_hf1_scrub.py: the registry is
    # emptied for the test and restored after it.
    monkeypatch.setattr(redact, "_literals", set())


def scrub_json(value):
    return json.loads(authproxy._scrub(json.dumps(value).encode()))


# ---------------------------------------------------------------------------
# E-mail addresses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("alice@example.com", MASK),
    ("contact alice@example.com now", f"contact {MASK} now"),
    ("Alice.B@Example.COM", MASK),
    ("(alice.b+tag@mail.example.co.uk).", f"({MASK})."),
    ("<bob_smith-1%x@sub-1.example.org>,", f"<{MASK}>,"),
    ("mailto:carol@ex.io;", f"mailto:{MASK};"),
    ("write to dave@example.com.", f"write to {MASK}."),
    ("owner=erin@example.net&x=1", f"owner={MASK}&x=1"),
    ("\"frank@example.com\"", f"\"{MASK}\""),
    ("a@x.io, b@y.io", f"{MASK}, {MASK}"),
    ("user+a.b-c_d%e@example.com", MASK),
], ids=["bare", "in-sentence", "case", "parens-plus-subdomains", "angle-underscore-pct",
        "mailto-semicolon", "trailing-dot", "query", "quoted", "two", "all-local-chars"])
def test_ap1_email_in_a_json_value_is_masked_whole_and_only_it(text, expected):
    assert scrub_json({"error": {"message": text}}) == {"error": {"message": expected}}


@pytest.mark.parametrize("text", [
    "user@host",               # no dot in the domain
    "alice@example.c",         # last label one letter
    "alice@example.c0m",       # last label not letters
    "alice@123.456",           # last label digits
    "@example.com",            # empty local part
    "alice@",                  # no domain
    "a @ b.com",               # spaces around @
    "alice@.com",              # empty label
    "ping @team about it",
], ids=["no-dot", "one-letter-tld", "digit-in-tld", "numeric", "empty-local", "no-domain",
        "spaced", "empty-label", "mention"])
def test_ap1_strings_that_are_not_email_addresses_are_left_alone(text):
    assert scrub_json({"message": text}) == {"message": text}
    raw = f"<p>{text}</p>".encode()
    assert authproxy._scrub(raw) == raw


def test_ap1_email_used_as_a_json_key_is_masked_in_the_key():
    assert scrub_json({"alice@example.com": {"role": "admin"}}) == {MASK: {"role": "admin"}}
    assert scrub_json({"owner alice@example.com": 1}) == {f"owner {MASK}": 1}


def test_ap1_email_in_a_non_json_page_is_masked():
    body = b"<html><body>Account alice@example.com is disabled.</body></html>"
    assert authproxy._scrub(body) == (
        f"<html><body>Account {MASK} is disabled.</body></html>".encode())


def test_ap1_email_in_non_utf8_text_keeps_every_byte_around_it():
    before, after = b"\xff\xc0<p>", b"</p>\xfe\x00"
    assert authproxy._scrub(before + b"alice@example.com" + after) == (
        before + MASK.encode() + after)


def test_ap1_email_inside_a_list_and_nested_values():
    value = {"errors": [{"detail": ["x", "billing contact: zed@corp.example.io!"]}]}
    assert scrub_json(value) == {"errors": [{"detail": ["x", f"billing contact: {MASK}!"]}]}


# ---------------------------------------------------------------------------
# Organisation and e-mail keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ORG_KEYS)
@pytest.mark.parametrize("value", [
    "MyCorp Inc", 1234567, 12.5, True, {"id": "org-1", "name": "MyCorp"}, ["MyCorp", 1],
], ids=["string", "int", "float", "bool", "object", "list"])
def test_ap1_identity_key_value_is_masked_whatever_its_shape_and_siblings_kept(key, value):
    body = {"type": "error",
            "error": {"type": "permission_error", "code": "denied",
                      "message": "not allowed", key: value}}
    assert scrub_json(body) == {
        "type": "error",
        "error": {"type": "permission_error", "code": "denied",
                  "message": "not allowed", key: MASK}}


@pytest.mark.parametrize("key", ["Organization", "ORG_ID", "Email", "User_Email",
                                 "ORGANIZATION_NAME", "oRg"])
def test_ap1_identity_key_match_is_case_insensitive(key):
    assert scrub_json({key: "MyCorp", "type": "x"}) == {key: MASK, "type": "x"}


def test_ap1_identity_key_at_top_level_and_in_a_list_of_objects():
    body = {"org_id": "o-1", "members": [{"email": "a@x.io", "role": "r"},
                                         {"user_email": None, "role": "s"}]}
    assert scrub_json(body) == {"org_id": MASK, "members": [{"email": MASK, "role": "r"},
                                                            {"user_email": MASK, "role": "s"}]}


def test_ap1_siblings_are_still_scrubbed_as_usual():
    redact.register_literal(LITERAL)
    body = {"organization": "MyCorp",
            "message": f"key {LITERAL} for ops@corp.example.com",
            "access_token": "short"}
    assert scrub_json(body) == {"organization": MASK,
                                "message": f"key {MASK} for {MASK}",
                                "access_token": MASK}


@pytest.mark.parametrize("key", ["organizational_unit", "org_chart", "emails_sent",
                                 "sorg", "e_mail_count"])
def test_ap1_only_the_named_keys_are_masked_wholesale(key):
    assert scrub_json({key: "kept value"}) == {key: "kept value"}


# ---------------------------------------------------------------------------
# Registered literals as keys, and collisions (Decision 4's first leak)
# ---------------------------------------------------------------------------

def test_ap1_registered_literal_used_as_a_key_is_masked_in_the_key():
    redact.register_literal(LITERAL)
    result = authproxy._scrub(json.dumps({LITERAL: "v", f"pre-{LITERAL}": 2}).encode())
    assert LITERAL.encode() not in result
    assert json.loads(result) == {MASK: "v", f"pre-{MASK}": 2}


@pytest.mark.parametrize("order", ["first-second", "second-first"])
def test_ap1_two_keys_colliding_after_masking_keep_the_later_one(order):
    redact.register_literal(LITERAL)
    redact.register_literal(LITERAL_2)
    pairs = [(f"k-{LITERAL}", 1), (f"k-{LITERAL_2}", 2)]
    if order == "second-first":
        pairs.reverse()
    body = "{" + ", ".join(f"{json.dumps(k)}: {v}" for k, v in pairs) + "}"
    result = authproxy._scrub(body.encode())
    assert LITERAL.encode() not in result and LITERAL_2.encode() not in result
    assert json.loads(result) == {f"k-{MASK}": pairs[-1][1]}


def test_ap1_two_email_keys_colliding_keep_the_later_one():
    body = b'{"a@example.com": 1, "keep": 0, "b@example.com": 2}'
    assert json.loads(authproxy._scrub(body)) == {MASK: 2, "keep": 0}


# ---------------------------------------------------------------------------
# Depth (Decision 4's second leak)
# ---------------------------------------------------------------------------

def _nest_lists(leaf, depth):
    for _ in range(depth):
        leaf = [leaf]
    return leaf


def _nest_dicts(leaf, depth):
    for _ in range(depth):
        leaf = {"a": leaf}
    return leaf


@pytest.mark.parametrize("leaf,secret", [
    (LITERAL, LITERAL),
    ("owner alice@example.com", "alice@example.com"),
    ({"organization": "DeepCorp"}, "DeepCorp"),
], ids=["literal", "email", "org-key"])
@pytest.mark.parametrize("nest", [_nest_lists, _nest_dicts], ids=["lists", "dicts"])
def test_ap1_value_deeper_than_the_limit_never_passes_through_raw(leaf, secret, nest):
    redact.register_literal(LITERAL)
    result = authproxy._scrub(json.dumps(nest(leaf, 30)).encode())
    assert secret.encode() not in result
    parsed = json.loads(result)
    assert MASK in json.dumps(parsed)
    # The shallow structure is kept: only the over-deep value is replaced.
    node = parsed
    for _ in range(10):
        node = node[0] if isinstance(node, list) else node["a"]


def test_ap1_deep_value_masked_whole_even_without_anything_recognisable():
    result = authproxy._scrub(json.dumps(_nest_lists("plain words", 40)).encode())
    assert b"plain words" not in result
    assert MASK.encode() in result


def test_ap1_nesting_within_the_limit_is_unchanged():
    value = _nest_dicts({"message": "ordinary words", "n": 3}, 15)
    assert scrub_json(value) == value


# ---------------------------------------------------------------------------
# Nothing to mask: the same parsed value (green today, stays green)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    {"type": "error", "error": {"type": "invalid_request_error",
                                "message": "messages.0.content: Field required"},
     "request_id": "req_011CTx"},
    [1, 2.5, None, True, "@here", {"note": "user@host is not an address"}],
    "plain string",
], ids=["overloaded", "invalid-request", "mixed", "string"])
def test_ap1_body_with_nothing_to_mask_parses_to_the_same_value(value):
    assert scrub_json(value) == value


# ---------------------------------------------------------------------------
# redact.scrub is unchanged for every other caller (Decision 7)
# ---------------------------------------------------------------------------

def test_ap1_redact_scrub_does_not_touch_emails_or_org_keys_for_other_callers():
    value = {"email": "alice@example.com", "organization": "MyCorp",
             "note": "mail bob@example.org", "org_id": 7}
    assert redact.scrub(value) == value
    assert redact.scrub("ping alice@example.com") == "ping alice@example.com"


# ---------------------------------------------------------------------------
# Through the proxy (reverses test_char_c1_authproxy's PII passthrough)
# ---------------------------------------------------------------------------

def test_ap1_upstream_error_reaching_the_agent_carries_no_identity(tmp_path, monkeypatch):
    body = json.dumps({"type": "error", "error": {
        "type": "permission_error", "message": "org admin@corp.example.com lacks access",
        "organization": "MyCorp", "organization_id": "0f3c-org", "email": "admin@corp.example.com",
    }}).encode()
    upstream = ap.Upstream(lambda req: ap.Reply(403, [("content-type", "application/json")], body))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "tok"}, upstream_url=url) as proxy:
        status, _, received = ap.request(proxy)
    assert status == 403
    for secret in (b"MyCorp", b"0f3c-org", b"admin@corp.example.com"):
        assert secret not in received
    assert json.loads(received)["error"]["type"] == "permission_error"
