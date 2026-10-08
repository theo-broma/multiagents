"""OA-R1, OA-R2: the root's quota check and wait read the host profile it runs on.

Black box: `multiagents run --unattended` as a subprocess over a fake machine
(tests/support/oa_world.py). A fake docker (exits 0) lets the project use the
docker executor; the fake vault disagrees with the host profiles on purpose.
Long sleeps are virtual (tests/support/oa_site).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402

HOURS = 3600


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.add_provider("claude-b", account="b")
    w.project_set("", executor="docker")
    return w


def test_r1_claude_root_is_checked_on_the_default_host_profile(world):
    world.make_profile("claude", used=100, reset_in=7 * HOURS + 600)
    world.make_profile("claude-b", identity="bob@example.invalid", used=10)
    world.make_vault(None, used=0)          # the vault says claude is free
    world.make_vault("b", used=100)
    r = world.run(2)
    assert world.launches() == [], "the exhausted host profile must stop the launch"
    assert "claude" in r.text and "7h" in r.text, \
        "the refusal names the provider and the host profile's reset time"
    assert world.net_attempts() == ""


def test_r1_claude_b_root_is_checked_on_its_own_host_profile(world):
    world.root_config(provider="claude-b")
    world.make_profile("claude", used=100, reset_in=HOURS)
    world.make_profile("claude-b", identity="bob@example.invalid", used=10)
    world.make_vault(None, used=100)
    world.make_vault("b", used=100)         # vault exhausted, host profile is not
    world.run(2)
    assert world.cfgs() and set(world.cfgs()) == {"claude-b"}, \
        "launch proceeds on claude-b: its host profile has headroom"


def test_r1_refusal_for_claude_b_names_claude_b_and_its_reset(world):
    world.root_config(provider="claude-b")
    world.make_profile("claude", used=10)
    world.make_profile("claude-b", identity="bob@example.invalid", used=100,
                       reset_in=9 * HOURS + 600)
    world.make_vault(None, used=0)
    world.make_vault("b", used=0)
    r = world.run(2)
    assert world.launches() == []
    assert "claude-b" in r.text and "9h" in r.text


def test_r1_initializer_is_checked_the_same_way(world):
    """"Root" is the initializer too."""
    world.make_profile("claude", used=100, reset_in=7 * HOURS + 600)
    world.make_vault(None, used=0)
    r = world.cli("init-agent")
    assert world.launches() == []
    assert "claude" in r.text


def test_r1_local_executor_still_reads_the_host_profile(tmp_path):
    world = World(tmp_path)
    world.make_profile("claude", used=100, reset_in=7 * HOURS + 600)
    world.run(2)
    assert world.launches() == []


def test_r2_wait_uses_host_reset_not_the_vaults(world):
    """The vault reports 20 minutes, the host profile 3 hours."""
    world.make_profile("claude", used=40)
    world.make_vault(None, used=0, reset_in=1200)
    world.plan({"launches": [{"do": "limit",
                              "exhaust": {"used": 100, "reset_in": 3 * HOURS}}]})
    world.sleep_script([world.free_at(3 * HOURS + 120, "claude")])
    world.run(3)
    launches = world.launches()
    assert len(launches) >= 2
    assert launches[1]["v"] >= 2.5 * HOURS, \
        f"relaunched after {launches[1]['v']}s of waiting; the host profile resets in 3h"


def test_r2_still_exhausted_at_wakeup_is_not_relaunched(world):
    """The timestamp passes, the account is still spent: wait again."""
    world.make_profile("claude", used=40)
    world.plan({"launches": [{"do": "limit",
                              "exhaust": {"used": 100, "reset_in": HOURS}}]})
    # At the first wake-up the profile reports a LATER reset; only much later is it free.
    world.sleep_script([
        {"after": HOURS, "profiles": [{"state": str(world.state_file("claude")),
                                       "used": 100, "reset_in": 2 * HOURS}]},
        world.free_at(3 * HOURS + 600, "claude")])
    world.run(3)
    launches = world.launches()
    assert len(launches) >= 2
    assert launches[1]["v"] >= 3 * HOURS, \
        "relaunched while the profile still reported no headroom"
    assert len(world.sleeps()) >= 2, "it waited again after the first wake-up"
