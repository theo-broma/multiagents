"""Contract BR: a burst is not a burn rate (ticket bug-c050b0).

The contract is `context/specs/burn-rate-baseline.md`, BR-R1 to BR-R5. Black
box throughout:

- the projection is read through `Tree.burn()`, over a headroom series written
  into the tree the way the existing burn-rate tests in `test_core.py` write it
  (timestamps are relative to the moment of writing, so time is under the
  test's control without sleeping for minutes);
- the wind-down is driven through `Runner._wind_down(budgets)` and observed on
  the budgets it was handed, and through `Runner.start()` and observed as the
  tree reports it (cooldowns, pause);
- the wrap-up is driven by real runs of a fake CLI (a small Python script
  standing in for the provider binary), with `wind_down_poll_seconds` made
  tiny so many watcher passes fit into a couple of seconds. What is observed is
  the project's event log (`wrap_up`, `steered`) and the prompts the runner
  handed the CLI (`runs/<id>/prompt*.md`) — the thing the agent actually saw.

The fake provider is called `p`: it has no budget action and no built-in
reader, so the runner's own headroom readings are `None` and never disturb the
series a test has written.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h                                # noqa: E402
from multiagents.budget import Budget                             # noqa: E402
from multiagents.config import Config                             # noqa: E402
from multiagents.runner import WRAP_UP, Runner                    # noqa: E402
from multiagents.tree import Tree                                 # noqa: E402

SID = "sess-br"
SHIPPED_PROJECT = (Path(__file__).resolve().parents[1]
                   / "src" / "multiagents" / "defaults" / "project.yaml")
# The part of WRAP_UP before its first placeholder: present in every wrap-up
# message whatever the provider and minutes.
WRAP_UP_MARK = WRAP_UP.split("{", 1)[0]
assert len(WRAP_UP_MARK) > 10


# ==========================================================================
# Headroom series
# ==========================================================================

def _anchor() -> float:
    """A whole-second "now", so spans written as integers are exact."""
    return float(int(time.time()))


def seed(tree: Tree, provider: str, points, anchor: float | None = None) -> None:
    """Replace `provider`'s headroom series with `points`: (seconds_ago, headroom)."""
    t0 = _anchor() if anchor is None else anchor
    with tree.transaction() as data:
        data.setdefault("headroom", {})[provider] = [
            [t0 - ago, headroom, 0.0] for ago, headroom in points]


# The ticket, as it stood when the watchers acted: 0.60 then 0.49 39 s later,
# everything older expired (the 0.80 reading is more than an hour old).
TICKET_MOMENT = [(4000, 0.80), (39, 0.60), (0, 0.49)]
# The ticket's series as the contract states it: 0.60 at t, 0.49 at t+39 s,
# t+76 s and t+107 s, older samples expired.
TICKET_SERIES = [(4000, 0.80), (107, 0.60), (68, 0.49), (31, 0.49), (0, 0.49)]


def steady_drain(start: float, points_per_minute: float = 5.0,
                 minutes: int = 6) -> list[tuple[int, float]]:
    """One reading a minute for `minutes` minutes, falling linearly."""
    return [(60 * (minutes - k), round(start - points_per_minute / 100 * k, 4))
            for k in range(minutes + 1)]


def linear_wall(headroom: float, points_per_minute: float) -> float:
    return headroom * 100 / points_per_minute * 60


# ==========================================================================
# BR-R1 — no projection from a short observation
# ==========================================================================

def _tree(tmp_path) -> Tree:
    return h.make_tree(tmp_path / "proj")


def test_br_r1_the_tickets_series_yields_no_projection(tmp_path):
    tree = _tree(tmp_path)
    seed(tree, "p", TICKET_SERIES)
    burn = tree.burn("p")
    assert "seconds_to_wall" not in burn, burn
    # It still says what it has.
    assert burn.get("samples") == 4, burn
    assert burn.get("headroom") == pytest.approx(0.49), burn


def test_br_r1_the_tickets_39_second_burst_yields_no_projection(tmp_path):
    """The two readings the 174 s wall was actually projected from."""
    tree = _tree(tmp_path)
    seed(tree, "p", TICKET_MOMENT)
    burn = tree.burn("p")
    assert "seconds_to_wall" not in burn, burn
    assert burn.get("samples") == 2, burn
    assert burn.get("headroom") == pytest.approx(0.49), burn


def test_br_r1_a_steady_drain_is_projected_within_ten_percent(tmp_path):
    tree = _tree(tmp_path)
    series = steady_drain(0.80, 5.0, 6)             # 0.80 -> 0.50 over 6 min
    seed(tree, "p", series)
    burn = tree.burn("p")
    assert "seconds_to_wall" in burn, burn
    expected = linear_wall(series[-1][1], 5.0)      # 600 s
    assert burn["seconds_to_wall"] == pytest.approx(expected, rel=0.10), burn


def test_br_r1_a_span_of_exactly_the_minimum_is_enough(tmp_path):
    """300 s and 3 samples: both minimums met exactly ("at least")."""
    tree = _tree(tmp_path)
    seed(tree, "p", [(300, 0.80), (150, 0.70), (0, 0.60)])
    burn = tree.burn("p")
    assert "seconds_to_wall" in burn, burn
    assert burn["seconds_to_wall"] == pytest.approx(linear_wall(0.60, 4.0), rel=0.10)


def test_br_r1_a_span_one_second_short_is_not_enough(tmp_path):
    """Plenty of samples, but they span 299 s."""
    tree = _tree(tmp_path)
    points = [(299 - 23 * k, round(0.80 - 0.02 * k, 4)) for k in range(13)] + [(0, 0.50)]
    seed(tree, "p", points)
    burn = tree.burn("p")
    assert "seconds_to_wall" not in burn, burn


def test_br_r1_two_samples_are_not_enough_however_far_apart(tmp_path):
    """A long span does not make up for too few readings."""
    tree = _tree(tmp_path)
    seed(tree, "p", [(1800, 0.90), (0, 0.30)])
    burn = tree.burn("p")
    assert "seconds_to_wall" not in burn, burn
    assert burn.get("samples") == 2


def test_br_r1_an_expired_sample_does_not_count_toward_the_minimums(tmp_path):
    """Three readings, but the oldest is outside the hour: what is USED is two
    readings 100 s apart."""
    tree = _tree(tmp_path)
    seed(tree, "p", [(3700, 0.90), (100, 0.60), (0, 0.50)])
    burn = tree.burn("p")
    assert "seconds_to_wall" not in burn, burn


# ==========================================================================
# Runner helpers
# ==========================================================================

def bare_runner(tmp_path, project: dict) -> Runner:
    """A Runner with a tree and a config and nothing else, as test_core.py
    builds one to drive `_wind_down`."""
    paths = h.make_paths(tmp_path / "proj")
    runner = Runner.__new__(Runner)
    runner.tree = Tree(paths.tree_file, paths.events_file)
    runner.config = Config(project=project, providers={}, agents={}, models={},
                           instruction_dirs=[])
    return runner


def wound_down(budget: Budget) -> bool:
    return bool(budget.cooldown_until) or not budget.usable


# Every invocation of the fake CLI announces its session, then stays alive
# until it is stopped — a run that is going, which is the only kind the
# wrap-up watcher acts on.
_CLI = r'''#!{python}
import json, sys, time
from pathlib import Path
count = Path({probe!r}) / "invocations"
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
print(json.dumps({{"type": "text", "session_id": {sid!r}, "text": "working"}}))
sys.stdout.flush()
time.sleep(60)
'''


def fake_provider(base: Path) -> tuple[dict, Path]:
    probe = base / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    script = base / "cli.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe), sid=SID))
    script.chmod(0o755)
    return {
        "bin": str(script),
        "spawn": {"args": ["--fake"], "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }, probe


FAST = {"wind_down_poll_seconds": 0.1}


def live_runner(tmp_path, monkeypatch, *, limits=None, agents=("worker",)):
    prov, probe = fake_provider(tmp_path)
    specs = {name: h.AgentSpec(name, "p", "m") for name in agents}
    r = h.make_runner(tmp_path / "proj", monkeypatch, agents=specs,
                      providers={"p": prov},
                      project={"limits": {**FAST, **(limits or {})}})
    return r, probe


def events(r) -> list[dict]:
    path = r.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def wrap_ups(r, agent_id) -> list[dict]:
    return [e for e in events(r)
            if e.get("kind") == "wrap_up" and e.get("agent") == agent_id]


def wrap_up_prompts(r, agent_id) -> list[str]:
    """The prompts this node's CLI was handed that carry the WRAP_UP text."""
    run_dir = r.paths.run_dir(agent_id)
    return [p.name for p in sorted(run_dir.glob("prompt*.md"))
            if WRAP_UP_MARK in p.read_text()]


def wrap_up_steers(r, agent_id) -> list[dict]:
    return [e for e in events(r)
            if e.get("kind") == "steered" and e.get("agent") == agent_id
            and WRAP_UP_MARK[:40] in (e.get("message") or "")]


async def until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def started(r, name="worker") -> str:
    res = await r.start(name, "go")
    agent = res["agent_id"]
    assert await until(lambda: bool(getattr(r.tree.get(agent), "session_id", "")), 15), \
        "the fake CLI never announced its session"
    return agent


async def stop_all(r):
    for agent_id in list(r.runs):
        run = r.runs[agent_id]
        if run.task and not run.task.done():
            try:
                await r.stop(agent_id)
            except Exception:
                pass


# ==========================================================================
# BR-R2 — nothing winds down or wraps up without a projection
# ==========================================================================

@pytest.mark.parametrize("series", [TICKET_SERIES, TICKET_MOMENT],
                         ids=["ticket-series", "ticket-39s-burst"])
def test_br_r2_the_wind_down_does_nothing_on_the_tickets_series(tmp_path, series):
    runner = bare_runner(tmp_path, {"limits": {"wind_down_seconds": 600,
                                               "wrap_up_seconds": 420}})
    seed(runner.tree, "p", series)
    budgets = {"p": Budget("p", known=True, headroom=0.49)}
    runner._wind_down(budgets)
    assert not wound_down(budgets["p"]), budgets["p"].note
    assert runner.tree.cooldown("p") is None
    assert runner.tree.pause_state() == {}


def test_br_r2_a_spawn_on_the_tickets_series_is_neither_cooled_nor_paused(
        tmp_path, monkeypatch):
    """The ticket's actual harm: a burst cooled the provider down and paused the
    tree. Observed through `start()`, the path that routes new work."""
    r, _ = live_runner(tmp_path, monkeypatch,
                       limits={"wind_down_seconds": 600, "wrap_up_seconds": 420})

    async def go():
        seed(r.tree, "p", TICKET_MOMENT)
        res = await r.start("worker", "go")
        try:
            return res, r.tree.cooldown("p"), r.tree.pause_state()
        finally:
            await stop_all(r)

    res, cooldown, pause = asyncio.run(go())
    assert cooldown is None, cooldown
    assert pause == {}, pause
    node = r.tree.get(res.get("agent_id", ""))
    assert node is not None and node.provider == "p", res


@pytest.mark.parametrize("series", [TICKET_SERIES, TICKET_MOMENT],
                         ids=["ticket-series", "ticket-39s-burst"])
def test_br_r2_the_wrap_up_watcher_does_nothing_on_the_tickets_series(
        tmp_path, monkeypatch, series):
    r, probe = live_runner(tmp_path, monkeypatch,
                           limits={"wind_down_seconds": 600, "wrap_up_seconds": 420})

    async def go():
        agent = await started(r)
        try:
            # Many watcher passes, each with the burst freshly in the window.
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                seed(r.tree, "p", series)
                await asyncio.sleep(0.1)
            return agent, wrap_ups(r, agent), wrap_up_prompts(r, agent), \
                (probe / "invocations").read_text()
        finally:
            await stop_all(r)

    agent, asked, prompts, invocations = asyncio.run(go())
    assert asked == [], asked
    assert prompts == [], prompts
    assert invocations == "1", "the run must not have been steered"
    assert r.tree.cooldown("p") is None
    assert r.tree.pause_state() == {}


# ==========================================================================
# BR-R3 — a genuine drain still triggers with the full lead
# ==========================================================================

# 0.50 -> 0.20 over six minutes at 5 points a minute: a wall in ~240 s, inside
# both the wind-down (600) and the wrap-up (420) lead.
GENUINE = steady_drain(0.50, 5.0, 6)


def test_br_r3_a_genuine_drain_winds_down_as_today(tmp_path):
    runner = bare_runner(tmp_path, {"limits": {"wind_down_seconds": 600,
                                               "wrap_up_seconds": 420}})
    seed(runner.tree, "p", GENUINE)
    before = time.time()
    budgets = {"p": Budget("p", known=True, headroom=0.20)}
    runner._wind_down(budgets)
    budget = budgets["p"]
    assert budget.usable is False
    assert "winding down" in budget.note
    left = linear_wall(0.20, 5.0)
    # Cooled for the time left to the wall (at least a minute), as today.
    assert budget.cooldown_until - before == pytest.approx(max(60.0, left), rel=0.10)


def test_br_r3_a_genuine_drain_asks_the_running_agent_to_wrap_up(tmp_path, monkeypatch):
    r, _ = live_runner(tmp_path, monkeypatch,
                       limits={"wind_down_seconds": 600, "wrap_up_seconds": 420})

    async def go():
        agent = await started(r)
        try:
            anchor = _anchor()
            seed(r.tree, "p", GENUINE, anchor)
            await until(lambda: wrap_up_steers(r, agent), 10)
            return agent, wrap_ups(r, agent), wrap_up_steers(r, agent)
        finally:
            await stop_all(r)

    agent, asked, steers = asyncio.run(go())
    assert asked, "a genuine drain inside the lead must ask for a wrap-up"
    first = asked[0]
    assert first.get("provider") == "p"
    assert first.get("seconds_left") == pytest.approx(linear_wall(0.20, 5.0), rel=0.10)
    assert steers, "and the ask reaches the agent as a steer"
    assert wrap_up_prompts(r, agent), "the resumed turn is handed the WRAP_UP text"


# ==========================================================================
# BR-R4 — the keys are read safely
# ==========================================================================

def _wind_down_on(tmp_path, series, budget_section, headroom):
    project = {"limits": {"wind_down_seconds": 600, "wrap_up_seconds": 420}}
    if budget_section is not None:
        project["budget"] = budget_section
    runner = bare_runner(tmp_path, project)
    seed(runner.tree, "p", series)
    budgets = {"p": Budget("p", known=True, headroom=headroom)}
    runner._wind_down(budgets)
    return budgets["p"]


def test_br_r4_zero_restores_projection_from_two_samples(tmp_path):
    budget = _wind_down_on(tmp_path, TICKET_MOMENT,
                           {"burn_min_span_seconds": 0, "burn_min_samples": 0}, 0.49)
    assert wound_down(budget), "0 means no minimum: today's behaviour"


def test_br_r4_configured_minimums_are_honoured(tmp_path):
    """Without this, every "falls back to the default" test below would pass
    against an implementation that never reads the keys at all."""
    loose = _wind_down_on(tmp_path / "a", TICKET_MOMENT,
                          {"burn_min_span_seconds": 30, "burn_min_samples": 2}, 0.49)
    assert wound_down(loose), "39 s and 2 samples meet a 30 s / 2 sample minimum"
    strict = _wind_down_on(tmp_path / "b", GENUINE,
                           {"burn_min_span_seconds": 900, "burn_min_samples": 3}, 0.20)
    assert not wound_down(strict), "a 6-minute drain does not meet a 15-minute minimum"
    few = _wind_down_on(tmp_path / "c", GENUINE,
                        {"burn_min_span_seconds": 300, "burn_min_samples": 20}, 0.20)
    assert not wound_down(few), "7 samples do not meet a 20 sample minimum"


MALFORMED = ["x", -1, None, "", True, float("nan"), float("inf"), [300], {"a": 1}]


@pytest.mark.parametrize("key", ["burn_min_span_seconds", "burn_min_samples"])
@pytest.mark.parametrize("bad", MALFORMED, ids=repr)
def test_br_r4_a_malformed_value_behaves_as_the_default(tmp_path, key, bad):
    # Malformed is neither "no minimum" (the burst would project) ...
    burst = _wind_down_on(tmp_path / "a", TICKET_MOMENT, {key: bad}, 0.49)
    assert not wound_down(burst), f"{key}={bad!r} must not mean 'no minimum'"
    # ... nor "never" (a genuine drain would not).
    drain = _wind_down_on(tmp_path / "b", GENUINE, {key: bad}, 0.20)
    assert wound_down(drain), f"{key}={bad!r} must not switch the projection off"


def test_br_r4_a_missing_or_null_budget_section_behaves_as_the_defaults(tmp_path):
    for sub, section in (("none", None), ("null", {"burn_min_span_seconds": None,
                                                    "burn_min_samples": None})):
        burst = _wind_down_on(tmp_path / sub / "a", TICKET_MOMENT, section, 0.49)
        assert not wound_down(burst), sub
        drain = _wind_down_on(tmp_path / sub / "b", GENUINE, section, 0.20)
        assert wound_down(drain), sub


def test_br_r4_the_keys_are_documented_in_the_shipped_defaults():
    text = SHIPPED_PROJECT.read_text()
    assert re.search(r"^\s*burn_min_span_seconds:\s*300\b", text, re.M), \
        "burn_min_span_seconds: 300 is shipped"
    assert re.search(r"^\s*burn_min_samples:\s*3\b", text, re.M), \
        "burn_min_samples: 3 is shipped"


# ==========================================================================
# BR-R5 — the wrap-up is asked once per agent, not once per run
# ==========================================================================

def test_br_r5_one_wrap_up_per_agent_through_steers_and_many_passes(
        tmp_path, monkeypatch):
    r, _ = live_runner(tmp_path, monkeypatch,
                       limits={"wind_down_seconds": 600, "wrap_up_seconds": 420})

    async def keep_draining(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            seed(r.tree, "p", GENUINE)
            await asyncio.sleep(0.1)

    async def go():
        agent = await started(r)
        try:
            seed(r.tree, "p", GENUINE)
            assert await until(lambda: wrap_ups(r, agent), 10), "never asked at all"
            # The wrap-up's own steer has replaced the run; let many passes of
            # whatever watches the replacement go by, still draining. (TS-R2:
            # 1 s is about ten passes at FAST's 0.1 s poll; it was 3 s.)
            await keep_draining(1.0)
            # And a steer from outside replaces it again.
            await r.steer(agent, "carry on")
            await keep_draining(1.0)
            return agent, wrap_ups(r, agent), wrap_up_steers(r, agent), \
                wrap_up_prompts(r, agent)
        finally:
            await stop_all(r)

    agent, asked, steers, prompts = asyncio.run(go())
    assert len(asked) == 1, f"{len(asked)} wrap_up events for one node"
    assert len(steers) == 1, f"{len(steers)} wrap-up steers for one node"
    assert len(prompts) == 1, f"the agent was handed the wrap-up {len(prompts)} times"


def test_br_r5_each_agent_is_asked_once_not_once_per_provider(tmp_path, monkeypatch):
    """Once per NODE: two agents draining the same provider are each asked."""
    r, _ = live_runner(tmp_path, monkeypatch,
                       limits={"wind_down_seconds": 600, "wrap_up_seconds": 420},
                       agents=("worker", "other"))

    async def go():
        a = await started(r, "worker")
        b = await started(r, "other")
        try:
            # TS-R2: drain until both have been asked (at most the 5 s this
            # used to wait out), then about ten more passes for a repeat.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (
                    wrap_up_prompts(r, a) and wrap_up_prompts(r, b)):
                seed(r.tree, "p", GENUINE)
                await asyncio.sleep(0.1)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                seed(r.tree, "p", GENUINE)
                await asyncio.sleep(0.1)
            return (wrap_up_prompts(r, a), wrap_up_prompts(r, b))
        finally:
            await stop_all(r)

    first, second = asyncio.run(go())
    assert len(first) == 1, first
    assert len(second) == 1, second


def test_br_r5_a_recovered_window_allows_a_second_wrap_up(tmp_path, monkeypatch):
    """After a reading with more headroom than at the moment of asking (a window
    reset), and a new drain, the node may be asked again."""
    # A lead long enough that the new drain, from well above the old level,
    # is still inside it.
    r, _ = live_runner(tmp_path, monkeypatch,
                       limits={"wind_down_seconds": 1000, "wrap_up_seconds": 900})

    async def go():
        agent = await started(r)
        try:
            seed(r.tree, "p", GENUINE)                 # asked at ~0.20 left
            assert await until(lambda: wrap_ups(r, agent), 10), "never asked at all"
            await asyncio.sleep(1.0)
            once = len(wrap_up_prompts(r, agent))

            # The window resets and drains again, from 0.95: every reading is
            # above the 0.20 the ask was made at. The last one is a real reading.
            anchor = _anchor()
            new = [(ago + 25, level) for ago, level in steady_drain(0.95, 5.0, 6)]
            seed(r.tree, "p", new, anchor)             # 0.95 -> 0.65, ending 25 s ago
            r.tree.note_headroom("p", 0.63)
            # Work resumes on the node, as it would after a reset.
            await r.steer(agent, "the window has reset; carry on")
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and len(wrap_up_prompts(r, agent)) < 2:
                await asyncio.sleep(0.1)
            return once, wrap_ups(r, agent), wrap_up_prompts(r, agent)
        finally:
            await stop_all(r)

    once, asked, prompts = asyncio.run(go())
    assert once == 1, f"asked {once} times before the window recovered"
    assert len(asked) == 2, f"{len(asked)} wrap_up events; expected a second after recovery"
    assert len(prompts) == 2, prompts
