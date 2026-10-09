"""OA-R8, positive case: a root limit pauses the agents on the root's account.

Contract: `context/specs/orchestrator-account.md`, OA-R8 and the decision
"Older contracts versus OA" (account equivalence). The root runs on the host
profile of provider `claude` (alice@example.invalid). A spawned agent of the
same provider name runs on the docker vault's default account. After the
driver has handled a root limit:

- the vault account is the same account as the root's (same identity): the
  agent's admission is refused;
- either identity is unknown: the routes count as the same account for
  pausing, the conservative side, so admission is refused;
- the vault is another account: admitted (the negative case lives in
  test_oa_isolation.py and is repeated here beside the positive ones).

The decision is the public `Runner.start(..., admission_only=True)` in a
subprocess over the fake machine; it is never patched. A marker only the
driver's own role reads does not satisfy these tests: admission must refuse.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402
from test_oa_isolation import admit  # noqa: E402

HOURS = 3600
ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


def vault_identity(w, who):
    """Rewrite the vault default account's identity, keeping its usage."""
    path = w.vault_dir(None) / ".claude.json"
    data = json.loads(path.read_text())
    if who is None:
        data.pop("oauthAccount", None)
    else:
        data["oauthAccount"] = {"emailAddress": who}
    path.write_text(json.dumps(data))


def world(tmp_path, host_identity=ALICE, vault=ALICE):
    w = World(tmp_path)
    w.project_set("", executor="docker")
    w.make_profile("claude", identity=host_identity, used=40)
    w.make_vault(None, used=0)                   # the agents' account has headroom
    vault_identity(w, vault)
    w.extra_env["OA_INTERRUPT_AT_SLEEP"] = "1"   # interrupt the driver's wait
    w.plan({"launches": [{"do": "limit", "exhaust": {"used": 100, "reset_in": 3 * HOURS}}],
            "default": {"do": "ok"}})
    return w


def refused(result):
    return result.get("admitted") is not True


def test_r8_control_the_agent_is_admitted_before_any_limit(tmp_path):
    w = world(tmp_path)
    assert admit(w, "implementer").get("admitted") is True


def test_r8_a_root_limit_pauses_an_agent_on_the_same_account(tmp_path):
    w = world(tmp_path, vault=ALICE)
    w.run(3)
    assert w.launches(), "the root ran and hit its limit"
    result = admit(w, "implementer")
    assert refused(result), f"same account as the root, yet admitted: {result}"


def test_r8_an_unknown_agent_identity_pauses_too(tmp_path):
    w = world(tmp_path, vault=None)
    w.run(3)
    assert w.launches()
    assert refused(admit(w, "implementer"))


def test_r8_an_unknown_root_identity_pauses_too(tmp_path):
    w = world(tmp_path, host_identity=None, vault=BOB)
    w.run(3)
    assert w.launches()
    assert refused(admit(w, "implementer"))


def test_r8_an_agent_on_another_account_still_runs(tmp_path):
    w = world(tmp_path, vault=BOB)
    w.run(3)
    assert w.launches()
    result = admit(w, "implementer")
    assert result.get("admitted") is True, result


def test_r8_no_limit_means_no_pause_even_on_the_same_account(tmp_path):
    w = world(tmp_path, vault=ALICE)
    w.plan({"default": {"do": "ok"}})
    w.run(2)
    assert w.launches()
    assert admit(w, "implementer").get("admitted") is True
