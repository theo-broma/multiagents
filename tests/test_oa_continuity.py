"""OA-R5: session continuity across a switch; OA-R6: the durable switch record.

Same fake machine as test_oa_switch.py. After the switch the target profile
either holds the session's transcript (install, then resume) or the driver
starts a fresh session with a recovery prompt. The fake CLI refuses
`--resume <id>` when the profile has no such transcript, as the real one does.
"""
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World, sha  # noqa: E402

HOURS = 3600
ALICE, BOB = "alice@example.invalid", "bob@example.invalid"
SID = "11111111-2222-3333-4444-555555555555"
LIMIT = {"do": "limit", "exhaust": {"used": 100, "reset_in": 3 * HOURS}}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.add_provider("claude-b")
    w.reserve(0.25)
    w.make_profile("claude", identity=ALICE, used=40)
    w.make_profile("claude-b", identity=BOB, used=10)
    w.root_config(switch_to=["claude-b"])
    d = w.proj / ".multiagents" / "launch"
    d.mkdir(exist_ok=True)
    (d / "orchestrator.session").write_text(SID)
    w.plan({"launches": [LIMIT], "default": {"do": "ok", "say": "turn-on-b"}})
    return w


def source_transcript(w):
    return w.projects_dir("claude") / f"{SID}.jsonl"


def test_r5_missing_transcript_on_target_is_installed_then_resumed(world):
    r = world.run(3)
    first, second = world.launches()[:2]
    assert second["cfg"].endswith("claude-b")
    assert second["mode"] == "resume" and second["sid"] == SID
    assert second["existed"]
    assert second["text_at_launch"] == source_transcript(world).read_text(), \
        "the target holds the session's transcript, complete, when the CLI starts"
    notice = [l for l in r.text.splitlines() if "resum" in l.lower()]
    assert notice, f"the terminal says the session was resumed:\n{r.text}"


def test_r5_nothing_but_the_transcript_crosses_profiles(world):
    (world.home / ".claude" / "settings.json").parent.mkdir(exist_ok=True)
    (world.home / ".claude" / "settings.json").write_text('{"hooks": "alice-only"}')
    before = world.snapshot(world.profile("claude-b"))
    world.run(3)
    after = world.snapshot(world.profile("claude-b"))
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed, "the transcript was installed"
    assert all(k.startswith("projects/") for k in changed), changed
    for k in changed:
        data = (world.profile("claude-b") / k).read_text()
        assert "alice-only" not in data and "fake-token-claude" not in data


def test_r5_install_failure_starts_a_fresh_session_with_a_recovery_prompt(world):
    projects = world.profile("claude-b") / "projects"
    projects.mkdir(parents=True)
    projects.chmod(0o555)                      # the slug directory cannot be created
    try:
        r = world.run(3)
    finally:
        projects.chmod(0o755)
    second = world.launches()[1]
    assert second["cfg"].endswith("claude-b")
    assert second["sid"] != SID and second["mode"] == "new", "a new session id"
    prompt = second["prompt"].lower()
    for word in ("brief.md", "quota", "budget", "question"):
        assert word in prompt, f"recovery prompt lacks {word!r}: {prompt}"
    assert "agent tree" in prompt or "tree" in prompt
    notice = [l for l in r.text.splitlines()
              if "fresh" in l.lower() or "new session" in l.lower()]
    assert notice, r.text
    assert not list(projects.iterdir())


def test_r5_a_different_file_with_the_same_name_is_left_intact(world):
    world.seed_session("claude-b", SID, "something else entirely\n")
    target = world.projects_dir("claude-b") / f"{SID}.jsonl"
    world.run(3)
    assert target.read_text() == "something else entirely\n"
    second = world.launches()[1]
    assert second["sid"] != SID, "the diverged session is not resumed under the same id"


def test_r5_a_symlinked_project_directory_is_not_followed(world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = world.projects_dir("claude-b")
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"], "the switch itself happened"
    assert list(elsewhere.iterdir()) == [], "something was written through the symlink"


def test_r5_a_symlinked_transcript_is_not_followed(world, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    d = world.projects_dir("claude-b")
    d.mkdir(parents=True)
    (d / f"{SID}.jsonl").symlink_to(victim)
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"], "the switch itself happened"
    assert victim.read_text() == "precious\n"
    assert (d / f"{SID}.jsonl").is_symlink()


# ------------------------------------------------------------------ OA-R6

def test_r6_agents_yaml_is_never_rewritten(world):
    before = world.agents_yaml.read_bytes()
    mtime = world.agents_yaml.stat().st_mtime_ns
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert world.agents_yaml.read_bytes() == before
    assert world.agents_yaml.stat().st_mtime_ns == mtime


def test_r6_driver_killed_between_install_and_launch_is_recovered(world):
    world.extra_env["OA_CRASH_CFG"] = str(world.profile("claude-b"))
    crashed = world.run(3)
    assert crashed.code == 137, "the seam killed the driver at the target's launch"
    assert len(world.launches()) == 1
    world.extra_env.pop("OA_CRASH_CFG")
    world.sleep_script([world.free_at(3 * HOURS + 300, "claude")])
    r = world.run(3)
    assert "Traceback" not in r.text
    later = world.launches()[1:]
    assert later, "the later run launches the root, finishing or abandoning the switch"
    for l in later:
        assert l["sid"] == SID and l["mode"] == "resume" and l["existed"], \
            "the session was not lost or forked by the interrupted switch"


def test_r6_a_later_run_on_the_configured_provider_brings_the_newer_transcript_back(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    on_b = (world.projects_dir("claude-b") / f"{SID}.jsonl").read_text()
    assert "turn-on-b" in on_b
    n = len(world.launches())
    world.make_profile("claude", identity=ALICE, used=10)      # the account reset
    world.plan({"default": {"do": "ok", "say": "back-home"}})
    (world.fake / "count").write_text("0")
    world.run(2)
    later = world.launches()[n:]
    assert later and later[0]["cfg"] == "", "a manual run starts from the configured provider"
    assert later[0]["mode"] == "resume" and later[0]["sid"] == SID
    assert "turn-on-b" in later[0]["text_at_launch"], \
        "the configured provider's older copy was replaced by the newer transcript"


def test_r6_the_configured_provider_is_checked_as_in_r1_on_the_later_run(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    n = len(world.launches())
    r = world.run(2)                      # default still exhausted
    assert len(world.launches()) == n, "the exhausted configured provider is not launched"
    assert "claude" in r.text


# ------------------------------------------------- OA-R5 companions

OTHER = "aaaaaaaa-2222-3333-4444-555555555555"


def test_r5_the_same_named_companion_directory_is_installed_with_the_transcript(world):
    src = world.projects_dir("claude")
    src.mkdir(parents=True, exist_ok=True)
    (src / SID / "subagents").mkdir(parents=True)
    (src / SID / "subagents" / "agent-1.jsonl").write_text("sub\n")
    (src / SID / "tool-results.txt").write_text("results\n")
    world.run(3)
    dst = world.projects_dir("claude-b")
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert (dst / SID / "subagents" / "agent-1.jsonl").read_text() == "sub\n"
    assert (dst / SID / "tool-results.txt").read_text() == "results\n"


def test_r5_nothing_but_the_transcript_and_its_companion_is_installed(world):
    src = world.projects_dir("claude")
    src.mkdir(parents=True, exist_ok=True)
    (src / SID).mkdir()
    (src / SID / "x.txt").write_text("x\n")
    (src / f"{OTHER}.jsonl").write_text("another session\n")
    (src / OTHER).mkdir()
    (src / OTHER / "y.txt").write_text("y\n")
    (src / "notes.txt").write_text("stray\n")
    world.run(3)
    dst = world.projects_dir("claude-b")
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    names = sorted(p.name for p in dst.iterdir())
    assert names == sorted([SID, f"{SID}.jsonl"]), names


def test_r5_no_companion_directory_appears_when_the_source_has_none(world):
    world.run(3)
    dst = world.projects_dir("claude-b")
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert sorted(p.name for p in dst.iterdir()) == [f"{SID}.jsonl"]


# ------------------------------------------------- OA-R6 reporting

def status_line(world):
    r = world.cli("status")
    return r.out


def test_r6_status_names_the_roots_role_and_its_provider_after_a_switch(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    out = status_line(world)
    lines = [l for l in out.splitlines() if "orchestrator" in l and "claude-b" in l]
    assert lines, f"no status line names the role and the provider it runs on:\n{out}"
    assert ALICE not in out and BOB not in out and "example.invalid" not in out


def test_r6_status_names_the_configured_provider_when_there_was_no_switch(world):
    world.plan({"default": {"do": "ok"}})
    world.run(2)
    out = status_line(world)
    import re
    lines = [l for l in out.splitlines()
             if "orchestrator" in l and re.search(r"\bclaude\b(?!-)", l)]
    assert lines, f"no status line names the role and the provider:\n{out}"


# ------------------------------------- OA-R6 reporting: budget_status root

from support.oa_world import budget_status  # noqa: E402


def test_r6_budget_status_root_names_the_configured_provider_when_there_was_no_switch(world):
    world.plan({"default": {"do": "ok"}})
    world.run(2)
    assert set(world.cfgs()) == {"claude"}
    root = budget_status(world).get("root")
    assert root == {"role": "orchestrator", "provider": "claude"}, root


def test_r6_budget_status_root_names_the_source_until_the_resume_is_confirmed(world):
    # the seam kills the driver at the target's launch: the switch record never
    # gets past the install, so the resume is not confirmed
    world.extra_env["OA_CRASH_CFG"] = str(world.profile("claude-b"))
    crashed = world.run(3)
    assert crashed.code == 137
    assert len(world.launches()) == 1
    root = budget_status(world).get("root")
    assert root == {"role": "orchestrator", "provider": "claude"}, \
        f"the switch is not confirmed, the root is still reported on the source: {root}"


def test_r6_budget_status_root_names_the_target_once_the_switch_record_is_resumed(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert "turn-on-b" in (world.projects_dir("claude-b") / f"{SID}.jsonl").read_text(), \
        "the target ran a turn after the resume"
    root = budget_status(world).get("root")
    assert root == {"role": "orchestrator", "provider": "claude-b"}, root


def test_r6_budget_status_root_carries_no_identity(world):
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    status = budget_status(world)
    root = status.get("root")
    assert isinstance(root, dict), f"no root entry in budget_status: {sorted(status)}"
    assert set(root) == {"role", "provider"}, root
    import json
    blob = json.dumps(root)
    assert ALICE not in blob and BOB not in blob and "example.invalid" not in blob
    assert "@" not in blob
