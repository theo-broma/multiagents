"""OA-R8: switching the root preserves admission pauses on spent accounts."""
import time
import yaml

from multiagents.paths import ProjectPaths
from multiagents.tree import Tree
from test_oa_account_pause import ALICE, BOB, vault_identity, world
from test_oa_isolation import admit
from test_oa_switch import CARA, LIMIT_ON_DEFAULT, build


def test_switching_the_root_does_not_release_its_exhausted_agent_account(tmp_path):
    w = build(tmp_path, extra=())
    w.add_provider("claude-b", extra='    container_private_home: [".claude-b"]\n')
    w.project_set("", executor="docker")
    w.make_vault(None, used=0)
    vault_identity(w, ALICE)
    w.make_profile("claude-b", identity=BOB, used=20)
    result = w.run(2)
    assert w.cfgs() == ["claude", "claude-b"], result.text
    assert admit(w, "implementer").get("admitted") is not True


def test_a_second_limit_preserves_the_first_accounts_admission_pause(tmp_path):
    w = build(tmp_path, switch_to=("claude-b", "claude-c"), extra=())
    for name in ("claude-b", "claude-c"):
        w.add_provider(name, extra=f'    container_private_home: [".{name}"]\n')
    w.project_set("", executor="docker")
    w.make_vault(None, used=0)
    vault_identity(w, ALICE)
    w.make_profile("claude-b", identity=BOB, used=20)
    w.make_profile("claude-c", identity=CARA, used=10)
    w.plan({"launches": [LIMIT_ON_DEFAULT, LIMIT_ON_DEFAULT], "default": {"do": "ok"}})
    result = w.run(3)
    assert w.cfgs() == ["claude", "claude-b", "claude-c"], result.text
    assert admit(w, "implementer").get("admitted") is not True


def test_fresh_root_room_clears_an_alias_pause_on_the_same_account(tmp_path):
    w = build(tmp_path, extra=("claude-b",))
    w.make_profile("claude-b", identity=ALICE, used=20)
    w.plan({"default": {"do": "ok"}})
    paths = ProjectPaths(w.proj)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.pause(time.time() + 3600, "claude-b: usage limit", ["claude-b"], cause="quota")
    w.run(1)
    assert w.launches()
    assert not tree.pause_state()


def test_root_recovery_keeps_a_pause_on_an_unknown_agent_route(tmp_path):
    w = world(tmp_path)
    w.add_provider("claude-b", extra='    container_private_home: [".claude-b"]\n')
    w.run(3)
    assert admit(w, "implementer").get("admitted") is not True
    w.make_profile("claude", identity=ALICE, used=20)
    w.plan({"default": {"do": "ok"}})
    w.run(1)
    assert admit(w, "implementer").get("admitted") is True
    config = yaml.safe_load(w.agents_yaml.read_text())
    config["agents"]["implementer"]["provider"] = "claude-b"
    w.agents_yaml.write_text(yaml.safe_dump(config))
    assert admit(w, "implementer").get("admitted") is not True


def test_agent_account_refresh_clears_its_conservative_root_pause(tmp_path):
    w = world(tmp_path, vault=None)
    w.run(3)
    assert admit(w, "implementer").get("admitted") is not True
    result = w.cli("refresh-quota", "claude")
    assert result.code == 0, result.text
    assert admit(w, "implementer").get("admitted") is True
