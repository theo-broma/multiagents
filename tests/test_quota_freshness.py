"""Quota freshness — no refusal on a window that has already reset.

Contract: `context/specs/quota-freshness.md`, QF-R1 … QF-R6. Written before
the implementation, so this file is red until it lands.

Surfaces, all public: `budget.read_all` / `Budget` (what `run` consults), the
`multiagents` command line (`run`, `run --wait`, `refresh-quota`), and the
tree's `cooldown()` / `pause_state()` (what the runner's preflight reads).

Seams, all at a boundary — see `tests/support/qf_harness.py`: the usage
endpoint at `urlopen`, the clock at `time.time`, the CLI's own files, stub
provider scripts, and the launch of the real orchestrator CLI.

Two assumptions the contract leaves open, each confined to the harness:
- a cooldown or pause records its cause through a `cause=` keyword on
  `Tree.set_cooldown` / `Tree.pause`, and a quota record says `"quota"`;
- `quota_reset_margin_seconds` is read from `limits:` in `project.yaml`, like
  every other project limit.

Stamps are relative to the clock the code reads. Where a test needs time to
pass it uses `FakeClock`; everywhere else the real clock, with slack far
larger than a test's own runtime.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import qf_harness as h  # noqa: E402

from multiagents import budget  # noqa: E402
from multiagents.paths import global_config_dir  # noqa: E402
from multiagents.providers import Provider  # noqa: E402

HOUR = 3600.0
DAY = 86400.0
MARGIN = 120.0                      # the contract's default reset margin


# =================================================================== setup ==

@pytest.fixture(autouse=True)
def _clean_budget_cache():
    budget.invalidate_cache()
    yield
    budget.invalidate_cache()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Budget readers against disposable state: a claude home, a stubbed
    usage endpoint (unreachable until a test says otherwise) and one script
    provider, `qfs`, whose `budget` answer is whatever `script()` wrote."""
    w = SimpleNamespace()
    w.tmp = tmp_path
    w.home = h.ClaudeHome(monkeypatch, tmp_path / "home")
    w.endpoint = h.UsageEndpoint(monkeypatch, tmp_path / "usage.calls")
    w.project_config = tmp_path / "project-config"
    (w.project_config / "providers").mkdir(parents=True)
    (w.project_config / "providers" / "claude.sh").write_text("#!/bin/sh\nexit 64\n")
    reading = tmp_path / "qfs.json"
    (w.project_config / "providers" / "qfs.sh").write_text(
        f'#!/bin/sh\ncase "$1" in\nbudget) cat "{reading}"; exit 0 ;;\n'
        f'*) exit 64 ;;\nesac\n')
    w.config_dir = global_config_dir()
    w.qfs = Provider.from_dict("qfs", {"bin": "sh", "script": "qfs.sh"})

    def script(data: dict) -> None:
        reading.write_text(__import__("json").dumps(data) + "\n")

    def read(name: str = "claude", use_cache: bool = False):
        provider = w.qfs if name == "qfs" else None
        out = budget.read_all({name: provider}, None, w.config_dir,
                              w.project_config, {}, {}, use_cache=use_cache)
        return out[name]

    w.script, w.read = script, read
    return w


def _two_windows(past_pct, past_at, future_pct, future_at):
    return h.usage_payload(five_hour=(past_pct, past_at), seven_day=(future_pct, future_at))


def _assert_room(b, headroom, resets_at_ts=None):
    assert b.usable, f"expected usable, got {b.to_dict()}"
    assert b.known, b.to_dict()
    assert b.headroom == pytest.approx(headroom, abs=0.011), b.to_dict()
    if resets_at_ts is not None:
        assert b.resets_at and h.epoch(b.resets_at) == pytest.approx(resets_at_ts, abs=1), \
            f"resets_at should be recomputed from the remaining windows: {b.to_dict()}"


# ================================================================== QF-R1 ==
# A window past its reset does not count. Each cache layer separately.

def test_qf_r1_cli_cache_window_past_its_reset_does_not_count_without_a_fresh_read(world):
    now = time.time()
    seven_day = now + 3 * DAY
    world.home.logout()                           # no fresh read can be made
    world.home.cache(_two_windows(99, h.iso(now - 600), 30, h.iso(seven_day)),
                     fetched_at=now)
    _assert_room(world.read(), 0.70, seven_day)


def test_qf_r1_cli_cache_window_past_its_reset_is_replaced_by_a_fresh_read(world):
    now = time.time()
    world.home.login()
    world.home.cache(_two_windows(99, h.iso(now - 600), 30, h.iso(now + 3 * DAY)),
                     fetched_at=now)
    world.endpoint.set({"payload": _two_windows(5, h.iso(now + 5 * HOUR),
                                                20, h.iso(now + 3 * DAY))})
    _assert_room(world.read(), 0.80)


def _seed_shared_file(world, clock):
    """Put a 99 % reading in the shared usage file through a real fetch, with a
    reset 30 s ahead, then let the clock run 200 s: past the reset by 170 s
    (beyond the margin) and still inside SHARED_TTL (300 s)."""
    world.home.login()                            # no CLI cache: the shared file serves
    t0 = clock.now
    world.endpoint.set({"payload": _two_windows(99, h.iso(t0 + 30), 30, h.iso(t0 + 3 * DAY))})
    first = world.read()
    assert not first.usable, f"precondition: the seeded reading is full {first.to_dict()}"
    assert world.endpoint.calls == 1, "precondition: the seed was fetched"
    budget.invalidate_cache()
    clock.advance(200)
    return t0


def test_qf_r1_shared_file_window_past_its_reset_does_not_count_without_a_fresh_read(
        world, monkeypatch):
    clock = h.FakeClock(monkeypatch)
    t0 = _seed_shared_file(world, clock)
    world.endpoint.set({"unreachable": True})
    _assert_room(world.read(), 0.70, t0 + 3 * DAY)


def test_qf_r1_shared_file_window_past_its_reset_is_replaced_by_a_fresh_read(
        world, monkeypatch):
    clock = h.FakeClock(monkeypatch)
    t0 = _seed_shared_file(world, clock)
    world.endpoint.set({"payload": _two_windows(5, h.iso(t0 + 5 * HOUR),
                                                20, h.iso(t0 + 3 * DAY))})
    _assert_room(world.read(), 0.80)


def test_qf_r1_in_process_cache_window_past_its_reset_does_not_count(world, monkeypatch):
    clock = h.FakeClock(monkeypatch)
    t0 = clock.now
    weekly = t0 + 3 * DAY
    # 100 s past: inside the margin, so it still counts on the first read.
    world.script(h.script_reading({"rolling": (99, h.iso(t0 - 100)),
                                   "weekly": (40, h.iso(weekly))}))
    first = world.read("qfs", use_cache=True)
    assert not first.usable, f"precondition: inside the margin it counts {first.to_dict()}"
    clock.advance(50)                 # 150 s past now; inside the 60 s in-process TTL
    _assert_room(world.read("qfs", use_cache=True), 0.60, weekly)


def test_qf_r1_provider_script_window_past_its_reset_does_not_count(world):
    """No claude special-casing: a script's windows get the same rule."""
    now = time.time()
    weekly = now + 3 * DAY
    world.script(h.script_reading({"rolling": (99, h.iso(now - 600)),
                                   "weekly": (40, h.iso(weekly))}))
    _assert_room(world.read("qfs"), 0.60, weekly)


@pytest.mark.parametrize("source", ["claude_cli_cache", "provider_script"])
def test_qf_r1_a_past_window_beside_a_full_future_window_stays_unusable(world, source):
    now = time.time()
    if source == "claude_cli_cache":
        world.home.logout()
        world.home.cache(_two_windows(99, h.iso(now - 600), 99, h.iso(now + 3 * DAY)),
                         fetched_at=now)
        b = world.read()
    else:
        world.script(h.script_reading({"rolling": (99, h.iso(now - 600)),
                                       "weekly": (99, h.iso(now + 3 * DAY))}))
        b = world.read("qfs")
    assert not b.usable, b.to_dict()


@pytest.mark.parametrize("source", ["claude_cli_cache", "provider_script"])
def test_qf_r1_a_reset_60s_ago_is_inside_the_margin_and_still_counts(world, source):
    now = time.time()
    if source == "claude_cli_cache":
        world.home.logout()
        world.home.cache(_two_windows(99, h.iso(now - 60), 30, h.iso(now + 3 * DAY)),
                         fetched_at=now)
        b = world.read()
    else:
        world.script(h.script_reading({"rolling": (99, h.iso(now - 60)),
                                       "weekly": (30, h.iso(now + 3 * DAY))}))
        b = world.read("qfs")
    assert not b.usable, b.to_dict()


@pytest.mark.parametrize("ago, counts", [(MARGIN - 1, True), (MARGIN, False)])
def test_qf_r1_the_margin_boundary_at_least_120s_is_past(world, monkeypatch, ago, counts):
    """"lies at least quota_reset_margin_seconds before now": 119 s counts,
    exactly 120 s does not."""
    clock = h.FakeClock(monkeypatch)
    world.script(h.script_reading({"rolling": (99, h.iso(clock.now - ago)),
                                   "weekly": (30, h.iso(clock.now + 3 * DAY))}))
    b = world.read("qfs")
    assert b.usable is (not counts), b.to_dict()


@pytest.mark.parametrize("stamp", ["naive", "unparseable"])
@pytest.mark.parametrize("source", ["claude_cli_cache", "provider_script"])
def test_qf_r1_a_reset_without_timezone_or_unparseable_still_counts(world, stamp, source):
    now = time.time()
    past = h.naive(now - 3 * HOUR) if stamp == "naive" else "not a date"
    if source == "claude_cli_cache":
        world.home.logout()
        world.home.cache(_two_windows(99, past, 30, h.iso(now + 3 * DAY)), fetched_at=now)
        b = world.read()
    else:
        world.script(h.script_reading({"rolling": (99, past),
                                       "weekly": (30, h.iso(now + 3 * DAY))}))
        b = world.read("qfs")
    assert not b.usable, b.to_dict()


def test_qf_r1_a_window_with_no_reset_is_unaffected(world):
    now = time.time()
    world.script(h.script_reading({"rolling": (99, None),
                                   "weekly": (10, h.iso(now - 600))}))
    b = world.read("qfs")
    assert not b.usable, b.to_dict()


@pytest.mark.parametrize("margin, expected", [(None, 3), (30, 0)])
def test_qf_r1_the_margin_is_the_project_limit_quota_reset_margin_seconds(
        tmp_path, monkeypatch, capsys, margin, expected):
    """A reset 60 s ago counts under the default 120 s margin, and does not
    under a 30 s one set in project.yaml."""
    h.ClaudeHome(monkeypatch, tmp_path / "home")
    h.UsageEndpoint(monkeypatch, tmp_path / "usage.calls")
    limits = {"quota_reset_margin_seconds": margin} if margin is not None else None
    p = h.Project(tmp_path, monkeypatch, limits=limits)
    now = time.time()
    p.reading("qfp", h.script_reading({"weekly": (99, h.iso(now - 60))}))
    code, out = p.cli(capsys, "run")
    assert code == expected, out
    assert bool(p.launches) is (expected == 0), out


# ================================================================== QF-R2 ==
# A fresh read after a reset is attempted, and bounded.

def test_qf_r2_a_past_reset_inside_the_ttl_fetches_exactly_once(world, monkeypatch):
    clock = h.FakeClock(monkeypatch)
    t0 = _seed_shared_file(world, clock)
    world.endpoint.set({"payload": _two_windows(5, h.iso(t0 + 5 * HOUR),
                                                20, h.iso(t0 + 3 * DAY))})
    before = world.endpoint.calls
    world.read()
    world.read()
    assert world.endpoint.calls - before == 1


def test_qf_r2_the_shared_file_is_rewritten_with_the_fresh_result(world, monkeypatch):
    """Seen by another process, which does not fetch: the file carries it."""
    clock = h.FakeClock(monkeypatch)
    t0 = _seed_shared_file(world, clock)
    world.endpoint.set({"payload": _two_windows(5, h.iso(t0 + 5 * HOUR),
                                                20, h.iso(t0 + 3 * DAY))})
    world.read()
    calls = world.endpoint.calls
    other = h.run_reader({
        "work": str(world.tmp), "state_dir": str(h.os.environ["MULTIAGENTS_STATE_DIR"]),
        "config_dir": str(world.config_dir), "project_config": str(world.project_config),
        "claude_home": str(world.home.root), "calls_file": str(world.endpoint.calls_file),
        "endpoint": {"unreachable": True}, "clock": t0 + 210,
    })
    assert world.endpoint.calls == calls, "the second process had no reason to fetch"
    assert other["usable"] and other.get("headroom") == pytest.approx(0.80, abs=0.011), other


@pytest.mark.parametrize("status, retry_after", [(429, "3600"), (429, None), (503, "3600")])
def test_qf_r2_an_unelapsed_back_off_means_no_fetch_and_the_fallback(
        world, monkeypatch, status, retry_after):
    clock = h.FakeClock(monkeypatch)
    t0 = clock.now
    world.home.login()
    world.endpoint.set({"status": status, "retry_after": retry_after})
    world.read()                                   # the refusal that starts the back-off
    assert world.endpoint.calls == 1, "precondition: the refused fetch happened"
    budget.invalidate_cache()
    clock.advance(70)                              # past the 60 s bound, inside the back-off
    world.home.cache(_two_windows(99, h.iso(t0 - 600), 30, h.iso(t0 + 3 * DAY)),
                     fetched_at=clock.now)
    world.endpoint.set({"payload": _two_windows(5, h.iso(t0 + 5 * HOUR),
                                                20, h.iso(t0 + 3 * DAY))})
    b = world.read()
    assert world.endpoint.calls == 1, "a back-off that has not elapsed still wins"
    _assert_room(b, 0.70, t0 + 3 * DAY)


_FAILING = {"status": 500}


def _lagging(t0):
    """The endpoint answering with the same stale window — a lagging backend."""
    return {"payload": _two_windows(99, h.iso(t0 - 600), 30, h.iso(t0 + 3 * DAY))}


@pytest.mark.parametrize("answer", ["failing", "lagging"])
def test_qf_r2_repeated_reads_within_60s_fetch_once(world, monkeypatch, answer):
    clock = h.FakeClock(monkeypatch)
    t0 = clock.now
    world.home.login()
    world.home.cache(_two_windows(99, h.iso(t0 - 600), 30, h.iso(t0 + 3 * DAY)),
                     fetched_at=t0)
    world.endpoint.set(_FAILING if answer == "failing" else _lagging(t0))
    for step in (0, 20, 39):                       # t0, t0+20, t0+59
        clock.advance(step)
        budget.invalidate_cache()
        assert world.read().usable
    assert world.endpoint.calls == 1


def test_qf_r2_the_60s_bound_lapses_and_a_past_reset_fetches_again(world, monkeypatch):
    clock = h.FakeClock(monkeypatch)
    t0 = clock.now
    world.home.login()
    world.home.cache(_two_windows(99, h.iso(t0 - 600), 30, h.iso(t0 + 3 * DAY)),
                     fetched_at=t0)
    world.endpoint.set(_FAILING)
    world.read()
    assert world.endpoint.calls == 1, "a past reset inside the CLI cache's trust triggers a fetch"
    clock.advance(61)
    budget.invalidate_cache()
    world.read()
    assert world.endpoint.calls == 2


@pytest.mark.parametrize("answer", ["failing", "lagging"])
def test_qf_r2_two_processes_make_one_fetch_between_them(world, answer):
    now = time.time()
    world.home.login()
    world.home.cache(_two_windows(99, h.iso(now - 600), 30, h.iso(now + 3 * DAY)),
                     fetched_at=now)
    spec = {
        "work": str(world.tmp), "state_dir": str(h.os.environ["MULTIAGENTS_STATE_DIR"]),
        "config_dir": str(world.config_dir), "project_config": str(world.project_config),
        "claude_home": str(world.home.root), "calls_file": str(world.endpoint.calls_file),
        "endpoint": _FAILING if answer == "failing" else _lagging(now),
    }
    first = h.run_reader(spec)
    second = h.run_reader(spec)
    assert world.endpoint.calls == 1, f"first={first} second={second}"
    assert first["usable"] and second["usable"], (first, second)


# ================================================================== QF-R3 ==

def test_qf_r3_the_refusal_names_provider_window_reset_source_age_and_the_command(
        tmp_path, monkeypatch, capsys):
    home = h.ClaudeHome(monkeypatch, tmp_path / "home")
    h.UsageEndpoint(monkeypatch, tmp_path / "usage.calls")
    p = h.Project(tmp_path, monkeypatch, orchestrator="claude")
    now = time.time()
    reset = now + 3.5 * HOUR
    home.logout()
    home.cache(_two_windows(99, h.iso(reset), 40, h.iso(now + 4 * DAY)),
               fetched_at=now - 300)
    code, out = p.cli(capsys, "run")
    assert code == 3, out
    assert not p.launches
    assert "claude" in out
    assert re.search(r"five[_ ]hour|5[- ]?hour", out), f"the full window by name:\n{out}"
    assert h.mentions_absolute(out, reset), f"resets_at as a time:\n{out}"
    assert h.mentions_relative(out, reset - now), f"resets_at as a countdown:\n{out}"
    assert "cachedUsageUtilization" in out, f"the source of the reading:\n{out}"
    assert re.search(r"\b(29[5-9]|30[0-9])\s*(s|sec|secs|seconds)\b", out), \
        f"the reading's age in seconds (300):\n{out}"
    assert "refresh-quota" in out, out


# ================================================================== QF-R4 ==
# `multiagents refresh-quota [provider…]`.

@pytest.fixture
def two(tmp_path, monkeypatch):
    """qfp (the orchestrator's) and qfp2, both script providers."""
    home = h.ClaudeHome(monkeypatch, tmp_path / "home")
    endpoint = h.UsageEndpoint(monkeypatch, tmp_path / "usage.calls")
    p = h.Project(tmp_path, monkeypatch, script_providers=("qfp", "qfp2"))
    p.home, p.endpoint = home, endpoint
    p.now = time.time()
    p.room = h.script_reading({"weekly": (10, h.iso(p.now + 2 * DAY))})
    p.full = h.script_reading({"weekly": (99, h.iso(p.now + 2 * DAY))})
    return p


def test_qf_r4_clears_the_quota_cooldown_and_quota_pause_after_a_usable_read(two, capsys):
    tree = two.tree
    h.seed_cooldown(tree, "qfp", two.now + HOUR, "quota failure during run", cause=h.QUOTA)
    h.seed_pause(tree, ["qfp"], two.now + HOUR, "qfp: usage limit", cause=h.QUOTA)
    two.reading("qfp", two.room)
    code, out = two.cli(capsys, "refresh-quota", "qfp")
    assert code == 0, out
    assert two.script_calls("qfp") >= 1
    assert tree.cooldown("qfp") is None, out
    assert not h.paused_for(tree, "qfp"), out


def test_qf_r4_prints_one_line_per_provider_with_its_reading_and_what_was_cleared(
        two, capsys):
    h.seed_cooldown(two.tree, "qfp", two.now + HOUR, "quota failure during run",
                    cause=h.QUOTA)
    reset = two.now + 2 * DAY
    two.reading("qfp", h.script_reading({"weekly": (10, h.iso(reset)),
                                         "rolling": (4, h.iso(two.now + HOUR))}))
    code, out = two.cli(capsys, "refresh-quota", "qfp")
    assert code == 0, out
    lines = [line for line in out.splitlines() if re.search(r"\bqfp\b", line)]
    assert len(lines) == 1, f"one line for qfp:\n{out}"
    line = lines[0]
    assert re.search(r"\b90(\.0)?\s*%|\b0\.9\b", line), f"headroom:\n{line}"
    assert "weekly" in line, f"worst window:\n{line}"
    assert h.mentions_absolute(line, reset) or h.mentions_relative(line, reset - two.now), \
        f"reset:\n{line}"
    assert "stubsrc" in line, f"source:\n{line}"
    assert re.search(r"cooldown", line, re.I), f"what was cleared:\n{line}"


@pytest.mark.parametrize("kind", ["needs_login", "no_cause", "provider_down", "family"])
def test_qf_r4_a_non_quota_cooldown_survives_a_usable_read(two, capsys, kind):
    """A usage API answering proves the account has quota, not that the CLI
    works: auth, crash-loop and family cooldowns stay, and so does a record
    that carries no cause at all."""
    reason = {"needs_login": "qfp2 is not authenticated",
              "no_cause": "3 runs in a row failed",
              "provider_down": "3 runs in a row failed",
              "family": "qf: qfp2 and qfp both failed"}[kind]
    h.seed_cooldown(two.tree, "qfp2", two.now + HOUR, reason,
                    needs_login=kind == "needs_login",
                    cause=kind if kind in ("provider_down", "family") else None)
    two.reading("qfp", two.room)
    two.reading("qfp2", two.room)
    code, out = two.cli(capsys, "refresh-quota", "qfp", "qfp2")
    assert code == 0, out
    kept = two.tree.cooldown("qfp2")
    assert kept is not None and kept.get("reason") == reason, out


@pytest.mark.parametrize("kind", ["no_cause", "spend_limit"])
def test_qf_r4_a_non_quota_pause_survives_a_usable_read(two, capsys, kind):
    h.seed_pause(two.tree, ["qfp2"], two.now + HOUR, "qfp2: spend cap",
                 cause=None if kind == "no_cause" else kind)
    two.reading("qfp", two.room)
    two.reading("qfp2", two.room)
    code, out = two.cli(capsys, "refresh-quota", "qfp", "qfp2")
    assert code == 0, out
    assert h.paused_for(two.tree, "qfp2"), out


def test_qf_r4_no_clear_after_an_unusable_read(two, capsys):
    h.seed_cooldown(two.tree, "qfp", two.now + HOUR, "quota failure during run",
                    cause=h.QUOTA)
    h.seed_pause(two.tree, ["qfp"], two.now + HOUR, "qfp: usage limit", cause=h.QUOTA)
    two.reading("qfp", two.full)
    code, out = two.cli(capsys, "refresh-quota", "qfp")
    assert code == 3, out
    assert two.tree.cooldown("qfp") is not None, out
    assert h.paused_for(two.tree, "qfp"), out


@pytest.mark.parametrize("unknown", ["known_false", "script_fails"])
def test_qf_r4_no_clear_after_an_unknown_read(two, capsys, unknown):
    h.seed_cooldown(two.tree, "qfp2", two.now + HOUR, "quota failure during run",
                    cause=h.QUOTA)
    h.seed_pause(two.tree, ["qfp2"], two.now + HOUR, "qfp2: usage limit", cause=h.QUOTA)
    two.reading("qfp", two.room)
    if unknown == "known_false":
        two.reading("qfp2", {"known": False, "note": "no quota surface"})
    else:
        two.reading("qfp2", "boom", exit_code=1)
    code, out = two.cli(capsys, "refresh-quota", "qfp", "qfp2")
    assert code == 0, out
    assert two.tree.cooldown("qfp2") is not None, out
    assert h.paused_for(two.tree, "qfp2"), \
        f"a usable qfp must not lift a pause that is about qfp2:\n{out}"


def test_qf_r4_clears_only_the_providers_it_re_read(two, capsys):
    h.seed_cooldown(two.tree, "qfp2", two.now + HOUR, "quota failure during run",
                    cause=h.QUOTA)
    two.reading("qfp", two.room)
    two.reading("qfp2", two.room)
    code, out = two.cli(capsys, "refresh-quota", "qfp")
    assert code == 0, out
    assert two.tree.cooldown("qfp2") is not None, out


def test_qf_r4_with_no_names_reads_every_provider(two, capsys):
    two.home.login()
    two.endpoint.set({"payload": _two_windows(10, h.iso(two.now + HOUR),
                                              10, h.iso(two.now + DAY))})
    two.reading("qfp", two.room)
    two.reading("qfp2", two.room)
    code, out = two.cli(capsys, "refresh-quota")
    assert code == 0, out
    assert two.script_calls("qfp") >= 1 and two.script_calls("qfp2") >= 1
    assert two.endpoint.calls == 1
    for name in ("qfp", "qfp2", "claude"):
        assert re.search(rf"\b{name}\b", out), f"a line for {name}:\n{out}"


@pytest.fixture
def claude_project(tmp_path, monkeypatch):
    home = h.ClaudeHome(monkeypatch, tmp_path / "home")
    endpoint = h.UsageEndpoint(monkeypatch, tmp_path / "usage.calls")
    p = h.Project(tmp_path, monkeypatch, orchestrator="claude")
    p.home, p.endpoint = home, endpoint
    return p


def _read_claude_as_run_does():
    return budget.read_all({"claude": None}, None, global_config_dir(), None,
                           {}, {}, use_cache=True)["claude"]


def test_qf_r4_replaces_the_shared_file_and_the_in_process_cache(
        claude_project, monkeypatch, capsys):
    p = claude_project
    clock = h.FakeClock(monkeypatch)
    t0 = clock.now
    p.home.login()
    full = _two_windows(99, h.iso(t0 + 3 * HOUR), 30, h.iso(t0 + 3 * DAY))
    p.endpoint.set({"payload": full})
    assert not _read_claude_as_run_does().usable, "precondition: both layers hold 99 %"
    assert p.endpoint.calls == 1
    clock.advance(70)                                  # inside SHARED_TTL and _CACHE_TTL
    p.endpoint.set({"payload": _two_windows(10, h.iso(t0 + 3 * HOUR),
                                            30, h.iso(t0 + 3 * DAY))})
    code, out = p.cli(capsys, "refresh-quota", "claude")
    assert code == 0, out
    assert p.endpoint.calls == 2, "one forced fetch"
    after = _read_claude_as_run_does()
    assert p.endpoint.calls == 2
    _assert_room(after, 0.70)


def test_qf_r4_ignores_the_clis_cached_usage(claude_project, capsys):
    p = claude_project
    now = time.time()
    p.home.login()
    p.home.cache(_two_windows(5, h.iso(now + 3 * HOUR), 5, h.iso(now + 3 * DAY)),
                 fetched_at=now)
    p.endpoint.set({"payload": _two_windows(99, h.iso(now + 3 * HOUR),
                                            30, h.iso(now + 3 * DAY))})
    code, out = p.cli(capsys, "refresh-quota", "claude")
    assert p.endpoint.calls == 1, out
    assert code == 3, f"the account says 99 %, whatever the CLI's cache says:\n{out}"


@pytest.mark.parametrize("retry_after", ["3600", None])
def test_qf_r4_a_rate_limited_provider_is_reported_not_fetched(
        claude_project, monkeypatch, capsys, retry_after):
    p = claude_project
    clock = h.FakeClock(monkeypatch)
    p.home.login()
    p.endpoint.set({"status": 429, "retry_after": retry_after})
    _read_claude_as_run_does()
    assert p.endpoint.calls == 1, "precondition: the 429 that starts the back-off"
    budget.invalidate_cache()
    clock.advance(70)
    p.endpoint.set({"payload": _two_windows(10, h.iso(clock.now + HOUR),
                                            10, h.iso(clock.now + DAY))})
    code, out = p.cli(capsys, "refresh-quota", "claude")
    assert p.endpoint.calls == 1, out
    assert "not re-read: rate-limited until" in out, out


@pytest.mark.parametrize("reading, expected", [("room", 0), ("full", 3)])
def test_qf_r4_exit_code_follows_the_orchestrators_provider(two, capsys, reading, expected):
    two.reading("qfp", two.room if reading == "room" else two.full)
    code, out = two.cli(capsys, "refresh-quota", "qfp")
    assert code == expected, out


def test_qf_r4_exit_code_2_for_an_unknown_provider_name(two, capsys):
    two.reading("qfp", two.room)
    code, out = two.cli(capsys, "refresh-quota", "nosuch")
    assert code == 2, out
    assert "nosuch" in out, f"the unknown name is reported:\n{out}"


def test_qf_r4_changes_no_config_and_starts_nothing(two, capsys):
    two.reading("qfp", two.room)
    two.reading("qfp2", two.room)
    before = two.config_snapshot()
    code, out = two.cli(capsys, "refresh-quota", "qfp", "qfp2")
    assert code == 0, out
    assert two.config_snapshot() == before
    assert two.launches == []
    assert not two.tree.read().get("nodes"), "no agent was started"


# ================================================================== QF-R5 ==

def _wait_scenario(p, monkeypatch, *, recovers: bool):
    """claude orchestrator; the CLI's cache (fresh, trusted for 900 s) says
    99 % until t0+100; after that the account reports room — or does not."""
    clock = h.FakeClock(monkeypatch, patch_sleep=True, sleep_limit=3 * HOUR)
    t0 = clock.now
    reset = t0 + 100
    p.home.login()
    p.home.cache(_two_windows(99, h.iso(reset), 30, h.iso(t0 + 3 * DAY)), fetched_at=t0)

    def answer(_index):
        if time.time() < reset or not recovers:
            later = reset if time.time() < reset else t0 + 2 * DAY
            return {"payload": _two_windows(99, h.iso(later), 30, h.iso(t0 + 3 * DAY))}
        return {"payload": _two_windows(5, h.iso(t0 + 5 * HOUR), 30, h.iso(t0 + 3 * DAY))}

    p.endpoint.set(answer)
    return clock, reset


def test_qf_r5_wait_proceeds_within_one_poll_of_the_reset(claude_project, monkeypatch, capsys):
    p = claude_project
    clock, reset = _wait_scenario(p, monkeypatch, recovers=True)
    code, out = p.cli(capsys, "run", "--wait")
    assert code == 0 and p.launches, out
    poll = max([s for s in clock.slept if s >= 1] or [0])
    assert poll > 0, "precondition: the wait loop polled"
    assert p.launches[0]["at"] <= reset + MARGIN + poll, (
        f"launched {p.launches[0]['at'] - reset:.0f}s after the reset with a "
        f"{poll:.0f}s poll — it waited for a cache to expire\n{out}")


def test_qf_r5_wait_keeps_waiting_while_there_is_no_room_and_does_not_storm(
        claude_project, monkeypatch, capsys):
    p = claude_project
    clock, _ = _wait_scenario(p, monkeypatch, recovers=False)
    code, out = p.cli(capsys, "run", "--wait")
    assert code == 3 and not p.launches, out
    elapsed = clock.now - clock.start
    assert p.endpoint.calls <= elapsed // 60 + 1, \
        f"{p.endpoint.calls} fetches in {elapsed:.0f}s"


# ================================================================== QF-R6 ==

def test_qf_r6_run_startup_lifts_the_orchestrators_quota_pause(two, capsys):
    h.seed_pause(two.tree, ["qfp"], two.now + 6 * HOUR, "qfp: usage limit", cause=h.QUOTA)
    two.reading("qfp", two.room)
    code, out = two.cli(capsys, "run")
    assert code == 0 and two.launches, out
    assert not h.paused_for(two.tree, "qfp"), out


def test_qf_r6_wait_proceeding_lifts_the_quota_pause_before_the_first_spawn(
        claude_project, monkeypatch, capsys):
    """The runner's preflight refuses a spawn onto a provider the tree's pause
    names, so the pause must be gone by the time the orchestrator starts."""
    p = claude_project
    _clock, _reset = _wait_scenario(p, monkeypatch, recovers=True)
    h.seed_pause(p.tree, ["claude"], time.time() + 6 * HOUR, "claude: usage limit",
                 cause=h.QUOTA)
    code, out = p.cli(capsys, "run", "--wait")
    assert code == 0 and p.launches, out
    assert "claude" not in (p.launches[0]["pause"].get("providers") or []), \
        f"paused at launch: {p.launches[0]['pause']}"


@pytest.mark.parametrize("kind", ["no_cause", "spend_limit"])
def test_qf_r6_a_non_quota_pause_survives_run_startup(two, capsys, kind):
    h.seed_pause(two.tree, ["qfp"], two.now + 6 * HOUR, "qfp: spend cap",
                 cause=None if kind == "no_cause" else kind)
    two.reading("qfp", two.room)
    code, out = two.cli(capsys, "run")
    assert code == 0, out
    assert h.paused_for(two.tree, "qfp"), out


def test_qf_r6_a_quota_pause_on_another_provider_survives_run_startup(two, capsys):
    h.seed_pause(two.tree, ["qfp2"], two.now + 6 * HOUR, "qfp2: usage limit",
                 cause=h.QUOTA)
    two.reading("qfp", two.room)
    code, out = two.cli(capsys, "run")
    assert code == 0, out
    assert h.paused_for(two.tree, "qfp2"), out
