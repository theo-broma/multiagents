"""OA-R9: a switch touches no credential, no .claude.json, no login state.

The only cross-profile write is the transcript installation under projects/.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World, sha  # noqa: E402

ALICE, BOB = "alice@example.invalid", "bob@example.invalid"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.add_provider("claude-b")
    w.reserve(0.25)
    # 96% used: still launchable (above the 2% floor), and the fake CLI never
    # edits .claude.json, so any change below is the driver's.
    w.make_profile("claude", identity=ALICE, used=96)
    w.make_profile("claude-b", identity=BOB, used=10)
    w.root_config(switch_to=["claude-b"])
    (w.home / ".claude" / "settings.json").write_text('{"alice": true}')
    d = w.proj / ".multiagents" / "launch"
    d.mkdir(exist_ok=True)
    (d / "orchestrator.session").write_text(SID)
    w.plan({"launches": [{"do": "limit"}], "default": {"do": "ok"}})
    return w


def guarded(w):
    files = [w.state_file("claude"), w.creds_file("claude"), w.home / ".claude" / "settings.json",
             w.state_file("claude-b"), w.creds_file("claude-b"),
             w.profile("claude-b") / "settings.json"]
    return {str(f): sha(f) for f in files}


def test_r9_credentials_state_and_settings_are_byte_identical_after_a_switch(world):
    before = guarded(world)
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"], "a switch happened"
    assert guarded(world) == before


def test_r9_no_login_or_logout_is_ever_run(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    verbs = {" ".join(i["argv"][:2]) for i in world.invocations()}
    assert not any(v.startswith(("auth login", "auth logout", "login", "logout")) for v in verbs)


def test_r9_a_switch_reaches_no_network_and_leaves_the_default_profile_the_default(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert world.net_attempts() == ""
    assert not (world.home / ".claude" / "projects" / "x").exists()
    # the default login still names the default account
    assert ALICE in world.state_file("claude").read_text()
