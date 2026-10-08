"""OA-R4, OA-R7: the automatic account switch of the root, and when it must not happen.

Scenario shape (see tests/support/oa_world.py): the root runs headless
(`run --unattended`) on the default profile `claude`; the first launch ends
with the CLI's limit message in its own transcript; `switch_to` lists the
other host profiles. Launch records say which profile every later launch used.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402

HOURS = 3600
ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CARA = "cara@example.invalid"
LIMIT_ON_DEFAULT = {"do": "limit", "exhaust": {"used": 100, "reset_in": 3 * HOURS}}


def build(tmp_path, switch_to=("claude-b",), extra=("claude-b", "claude-c", "claude-d", "claude-e")):
    w = World(tmp_path)
    for name in extra:
        w.add_provider(name)
    w.reserve(0.25)
    w.make_profile("claude", identity=ALICE, used=40)
    if switch_to is not None:
        w.root_config(switch_to=list(switch_to))
    w.plan({"launches": [LIMIT_ON_DEFAULT], "default": {"do": "ok"}})
    return w


@pytest.fixture
def world(tmp_path):
    return build(tmp_path)


def test_r4_first_qualifying_candidate_is_chosen_in_list_order(tmp_path):
    w = build(tmp_path, switch_to=("claude-b", "claude-c"))
    w.make_profile("claude-b", identity=BOB, used=20)
    w.make_profile("claude-c", identity=CARA, used=5)
    w.run(3)
    assert w.cfgs()[:2] == ["claude", "claude-b"]
    assert w.sleeps() == [], "a switch is not a wait"


def test_r4_the_switch_resumes_the_same_session_on_the_target(world):
    world.make_profile("claude-b", identity=BOB, used=20)
    world.run(3)
    first, second = world.launches()[:2]
    assert second["cfg"].endswith("claude-b")
    assert second["sid"] == first["sid"] and second["mode"] == "resume"
    assert second["existed"], "the transcript was in the target profile when the CLI started"


@pytest.mark.parametrize("skipped", ["exhausted", "unknown", "stale", "expired-credentials",
                                     "below-reserve", "same-identity"])
def test_r4_unqualified_candidates_are_skipped(tmp_path, skipped):
    w = build(tmp_path, switch_to=("claude-b", "claude-c"))
    bad = {"exhausted": dict(identity=BOB, used=100),
           "unknown": dict(identity=BOB, used=None),
           "stale": dict(identity=BOB, used=10, age=3 * HOURS),
           "expired-credentials": dict(identity=BOB, used=10, creds="expired"),
           "below-reserve": dict(identity=BOB, used=76),
           "same-identity": dict(identity=ALICE, used=10)}[skipped]
    w.make_profile("claude-b", **bad)
    w.make_profile("claude-c", identity=CARA, used=10)
    w.run(3)
    assert w.cfgs()[:2] == ["claude", "claude-c"]


def test_r4_headroom_exactly_at_the_reserve_qualifies(tmp_path):
    w = build(tmp_path)
    w.make_profile("claude-b", identity=BOB, used=75)       # headroom 0.25 == reserve
    w.run(3)
    assert w.cfgs()[:2] == ["claude", "claude-b"]


def test_r4_an_unknown_identity_on_the_candidate_does_not_disqualify_it(tmp_path):
    w = build(tmp_path)
    w.make_profile("claude-b", identity=None, used=10)
    w.run(3)
    assert w.cfgs()[:2] == ["claude", "claude-b"]


def test_r4_reserve_fraction_is_the_configured_one(tmp_path):
    w = build(tmp_path, switch_to=("claude-b", "claude-c"))
    w.reserve(0.6)
    w.make_profile("claude-b", identity=BOB, used=50)             # headroom 0.5 < 0.6
    w.make_profile("claude-c", identity=CARA, used=30)            # headroom 0.7 >= 0.6
    w.run(3)
    assert w.cfgs()[:2] == ["claude", "claude-c"]


@pytest.mark.parametrize("switch_to", [None, []])
def test_r4_no_list_means_wait_then_relaunch_on_the_same_provider(tmp_path, switch_to):
    w = build(tmp_path, switch_to=switch_to)
    w.make_profile("claude-b", identity=BOB, used=5)          # plenty of room, never used
    w.sleep_script([w.free_at(3 * HOURS + 120, "claude")])
    w.run(3)
    assert set(w.cfgs()) == {"claude"}
    assert w.sleeps() and w.launches()[1]["v"] >= 2.5 * HOURS


def test_r4_all_candidates_exhausted_waits_for_the_earliest_reset_then_reselects(tmp_path):
    w = build(tmp_path, switch_to=("claude-b", "claude-c"))
    w.make_profile("claude-b", identity=BOB, used=100, reset_in=HOURS)
    w.make_profile("claude-c", identity=CARA, used=100, reset_in=2 * HOURS)
    w.sleep_script([w.free_at(HOURS + 120, "claude-b")])
    w.run(3)
    launches = w.launches()
    assert len(launches) >= 2
    assert launches[1]["cfg"].endswith("claude-b"), "re-selected the account that reset first"
    assert 0.8 * HOURS <= launches[1]["v"] < 2 * HOURS, \
        "it waited for the earliest reset among all of them, not the configured provider's"


def test_r4_a_target_that_hits_the_limit_at_once_is_not_retried_in_the_episode(tmp_path):
    w = build(tmp_path, switch_to=("claude-b",))
    w.make_profile("claude-b", identity=BOB, used=20)
    w.plan({"launches": [LIMIT_ON_DEFAULT,
                         {"do": "limit", "exhaust": {"used": 100, "reset_in": 2 * HOURS}}],
            "default": {"do": "ok"}})
    w.run(4)
    assert w.cfgs()[:2] == ["claude", "claude-b"]
    before_any_wait = [c for c, l in zip(w.cfgs(), w.launches()) if l["v"] == 0]
    assert before_any_wait == ["claude", "claude-b"], \
        f"a provider was tried twice in one episode: {before_any_wait}"
    assert w.sleeps(), "nobody qualified: the driver waits"


def test_r4_a_stale_marker_in_the_copied_transcript_does_not_trigger_a_switch(world):
    """The target's first launch writes nothing; the copy still ends with the old limit message."""
    world.make_profile("claude-b", identity=BOB, used=20)
    world.plan({"launches": [LIMIT_ON_DEFAULT, {"do": "silent"}], "default": {"do": "silent"}})
    world.run(3)
    assert world.cfgs()[:2] == ["claude", "claude-b"]
    assert set(world.cfgs()[1:]) == {"claude-b"}, "it switched again on evidence older than the launch"
    assert world.sleeps() == [], "and it waited on it"


def test_r4_another_sessions_limit_message_does_not_count(world):
    world.make_profile("claude-b", identity=BOB, used=20)
    world.plan({"launches": [{"do": "ok", "other_limit": True}], "default": {"do": "ok"}})
    world.run(3)
    assert set(world.cfgs()) == {"claude"}
    assert world.sleeps() == []


def test_r4_no_supervise_says_automatic_switching_is_unavailable(world):
    world.make_profile("claude-b", identity=BOB, used=20)
    r = world.cli("run", "--no-supervise")
    lines = [l for l in r.err.splitlines() if "switch" in l.lower()]
    assert lines and "unavailable" in " ".join(lines).lower(), r.err


def test_r4_a_switch_to_provider_of_another_cli_is_a_config_error_at_load(tmp_path):
    w = build(tmp_path, switch_to=("opencode",))
    r = w.run(2)
    assert r.code != 0
    assert w.launches() == []
    assert "switch_to" in r.text and "opencode" in r.text


def test_r4_a_switch_to_provider_that_does_not_exist_is_a_config_error(tmp_path):
    w = build(tmp_path, switch_to=("claude-nope",))
    r = w.run(2)
    assert r.code != 0 and w.launches() == []
    assert "switch_to" in r.text


def test_r4_switch_to_must_be_a_list(tmp_path):
    w = build(tmp_path, switch_to=None)
    text = w.agents_yaml.read_text()
    w.agents_yaml.write_text(text.replace("  orchestrator:\n    provider: claude\n",
                                          "  orchestrator:\n    provider: claude\n    switch_to: claude-b\n", 1))
    r = w.run(2)
    assert r.code != 0 and w.launches() == []


def test_r4_a_limit_that_never_resets_stops_the_driver_as_today(tmp_path):
    w = build(tmp_path, extra=("claude-b", "claude-s"))
    w.add_provider("claude-s", extra="    transcript:\n      limit_markers:\n"
                   "        - {match: \"hit your usage limit\", resets: false, detail: spend cap}\n")
    w.root_config(provider="claude-s", switch_to=["claude-b"])
    w.make_profile("claude-s", identity=ALICE, used=40)
    w.make_profile("claude-b", identity=BOB, used=10)
    r = w.run(3)
    assert r.code == 3
    assert w.cfgs() == ["claude-s"], "a spend limit is a stop, not a switch"


# ------------------------------------------------------------------ OA-R7

def test_r7_a_crash_exit_never_switches(world):
    world.make_profile("claude-b", identity=BOB, used=5)
    world.plan({"launches": [{"do": "fail"}, {"do": "fail"}, {"do": "fail"}]})
    world.run(4)
    assert set(world.cfgs()) == {"claude"}


def test_r7_a_clean_exit_never_switches(world):
    world.make_profile("claude-b", identity=BOB, used=5)
    world.plan({"default": {"do": "ok"}})
    world.run(3)
    assert set(world.cfgs()) == {"claude"} and world.sleeps() == []


def test_r7_a_crash_stops_after_three_turns_exactly_as_today(tmp_path):
    w = build(tmp_path)
    w.make_profile("claude-b", identity=BOB, used=5)
    w.plan({"default": {"do": "fail"}})
    r = w.run(6)
    assert len(w.launches()) == 3 and "three turns in a row failed" in r.out


# ------------------------------------------------- OA-R4: fresh episodes

def test_r4_the_configured_provider_is_considered_after_the_list(tmp_path):
    w = build(tmp_path)
    w.make_profile("claude-b", identity=BOB, used=100, reset_in=5 * HOURS)
    w.sleep_script([w.free_at(3 * HOURS + 120, "claude")])
    w.run(3)
    launches = w.launches()
    assert len(launches) >= 2
    assert launches[1]["cfg"] == "", "after the wait the configured provider is the one with headroom"
    assert 2.5 * HOURS <= launches[1]["v"] < 5 * HOURS


def test_r4_a_provider_tried_in_an_earlier_episode_is_eligible_again_after_a_wait(tmp_path):
    w = build(tmp_path)
    w.make_profile("claude-b", identity=BOB, used=20)
    w.plan({"launches": [LIMIT_ON_DEFAULT,
                         {"do": "limit", "exhaust": {"used": 100, "reset_in": 2 * HOURS}}],
            "default": {"do": "ok"}})
    w.sleep_script([w.free_at(2 * HOURS + 120, "claude-b")])
    w.run(4)
    cfgs = w.cfgs()
    assert cfgs[:3] == ["claude", "claude-b", "claude-b"], cfgs
    assert w.launches()[2]["v"] >= 1.5 * HOURS, "the second try on claude-b is a new episode, after the wait"
