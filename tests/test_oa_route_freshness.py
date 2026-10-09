"""OA-R8/QF-R6: recovery needs fresh quota on the agents' actual route."""
import json

import pytest
import yaml

from test_oa_account_pause import ALICE, BOB, vault_identity, world
from test_oa_isolation import admit


@pytest.mark.parametrize("used,age,recovered", [
    (100, 0, False),
    (99, 0, False),
    (20, 0, True),
    (20, 60, False),
])
def test_refresh_proves_the_default_route_healthy_even_with_a_healthy_spare(
        tmp_path, used, age, recovered):
    w = world(tmp_path)
    w.project_set("budget:\n  max_reading_age_seconds: 10\n")
    w.make_vault(None, used=used)
    vault_identity(w, ALICE)
    w.make_vault("spare", used=0)
    if age:
        state = w.vault_dir(None) / ".claude.json"
        data = json.loads(state.read_text())
        data["cachedUsageUtilization"]["fetchedAtMs"] -= age * 1000
        state.write_text(json.dumps(data))
    w.run(3)
    assert admit(w, "implementer").get("admitted") is not True
    result = w.cli("refresh-quota", "claude")
    assert result.code == 0, result.text
    assert (admit(w, "implementer").get("admitted") is True) == recovered


def test_an_aged_host_reading_does_not_clear_a_proven_same_account_pause(tmp_path):
    w = world(tmp_path)
    w.project_set("budget:\n  max_reading_age_seconds: 10\n")
    w.run(3)
    assert admit(w, "implementer").get("admitted") is not True
    w.make_profile("claude", identity=ALICE, used=20, age=60)
    w.plan({"default": {"do": "ok"}})
    w.run(1)
    assert admit(w, "implementer").get("admitted") is not True


@pytest.mark.parametrize("recovered", [False, True])
def test_local_refresh_reads_the_profile_selected_by_the_base_provider(tmp_path, recovered):
    w = world(tmp_path)
    w.project_set("executor:\n  kind: local\n")
    w.add_provider("claude-profile")
    w.make_profile("claude-profile", identity=ALICE, used=40)
    w.make_profile("claude", identity=BOB, used=100 if recovered else 20)
    path = w.proj / ".multiagents" / "config" / "providers.yaml"
    data = yaml.safe_load(path.read_text())
    data["providers"]["claude"]["env"] = {
        "CLAUDE_CONFIG_DIR": str(w.profile("claude-profile"))}
    path.write_text(yaml.safe_dump(data))
    w.run(3)
    assert admit(w, "implementer").get("admitted") is not True
    w.make_profile("claude-profile", identity=ALICE, used=20 if recovered else 100)
    result = w.cli("refresh-quota", "claude")
    assert result.code == (0 if recovered else 3), result.text
    assert (admit(w, "implementer").get("admitted") is True) == recovered
