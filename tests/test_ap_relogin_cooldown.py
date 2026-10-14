"""AP5 / F22: exhaustion describes the current login's cooldown."""

import sys
from pathlib import Path

from multiagents import authproxy

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ap_support as ap


def test_ap5_fresh_login_keeps_cooldown_and_gets_retry_after(tmp_path, monkeypatch):
    ap.FakeClock(monkeypatch)
    upstream = ap.Upstream(lambda req: ap.Reply(401, [], b'{"error":"expired"}'))
    with ap.upstream_server(upstream) as url, \
            ap.proxy_server(tmp_path, monkeypatch, accounts={"acc": "old-token"},
                            upstream_url=url) as proxy:
        assert ap.request(proxy)[0] == 401
        # A concurrent attempt can return a limit after the old token's 401.
        authproxy.Handler.accounts.mark_limited("acc", 120)
        ap.seed_account(proxy.vault, "acc", "new-token")

        status, headers, _ = ap.request(proxy, agent="agent-after-login")
        assert status == 429
        assert headers.get("retry-after") == "120"
        assert upstream.tokens() == ["old-token"]
