"""Adversary, slice 4C (D-S4C.2, IC-C33): what the guest can make the host
adapter do with the pinned account's real token.

The adapter attaches the vault's OAuth access token to ANY request target on
`https://api.anthropic.com`. That origin also serves the account endpoints the
vendor CLI itself uses with that token: `/api/oauth/usage` (budget.py reads it),
`/api/oauth/profile`, and the CLI's login-time
`/api/oauth/claude_cli/create_api_key`, which answers with a new, long-lived
API key. A guest that asks the gateway for that path walks away holding an
Anthropic credential that outlives the attempt, its grant and every
revocation, which is what D-S4C.2 ("no Anthropic token reaches an isolated
attempt") exists to prevent. Synthetic vault, recorded upstream; nothing is sent.
"""
import json

import pytest

from multiagents.scheduler.claude_credentials import ClaudeAdapter

ACCESS = "FICTION-ADV-GW-ACCESS-2f81"


@pytest.fixture
def adapter(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": ACCESS, "refreshToken": "FICTION-ADV-GW-REFRESH", "expiresAt": 4102444800000}}))
    sent = []
    a = ClaudeAdapter(tmp_path, pins={"claude": "default"}, upstream=lambda m, u, h, b: sent.append((m, u, h)))
    return a, sent


@pytest.mark.parametrize("path", [
    "/api/oauth/claude_cli/create_api_key",
    "/api/oauth/profile",
])
def test_adv_s4c_the_guest_cannot_spend_the_account_token_on_account_endpoints(adapter, path):
    a, sent = adapter
    try:
        a.open("POST", path, {"Content-Type": "application/json"}, b"{}", provider="claude", run_id="r1")
    except Exception:
        pass
    carried = [u for m, u, h in sent if ACCESS in json.dumps(h)]
    assert carried == [], f"the pinned account's access token was attached to {carried}"


def test_adv_s4c_guard_the_model_api_still_carries_the_token(adapter):
    a, sent = adapter
    a.open("POST", "/v1/messages", {}, b"{}", provider="claude", run_id="r1")
    assert sent and sent[0][2]["Authorization"] == "Bearer " + ACCESS
