"""A half-open trial belongs to the run that launches on its provider.

A tripped provider whose cooldown has lapsed is allowed exactly one trial
(`Tree.claim_trial`). The pre-routing pass (`Runner._half_open`) used to claim
that trial for EVERY lapsed provider on every real start — including starts
that launched on an unrelated healthy provider. The claim set `trial_at`, so
the next admission probe for the tripped provider saw `trial_pending` and
refused it for another minute. While other providers kept starting, the
tripped provider was therefore never admitted and the nodes pinned to it
stayed refused, until somebody cleared it by hand.

So the rule is: a start claims the half-open trial only for the provider it
will actually launch on, once routing has chosen and nothing can still refuse
it. Every other tripped provider is judged without spending its trial, and
the admission probe still never claims (bug-521be6).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.runner import LaunchContext  # noqa: E402


def _launches(marker: Path) -> int:
    """How many times either fake CLI has actually been spawned."""
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def _world(tmp_path, monkeypatch, *, delay=0.0):
    """Two fake providers, `pa` (the one that will trip) and `pb` (healthy).

    `w_a` runs on `pa`, `w_b` on `pb`, and neither names the other in
    `models:`, so routing for one can never land on the other — a start for
    `w_b` has no business touching `pa`'s breaker state. Every spawn of
    either CLI appends one line to `marker`, so "exactly one run" is a fact
    about the filesystem.
    """
    marker = tmp_path / "launches"
    events = [{"type": "text", "text": "ok"}]
    cli_a = h.fake_cli(tmp_path, "pa", events=events, delay=delay)
    cli_b = h.fake_cli(tmp_path, "pb", events=events, delay=delay)
    for cli in (cli_a, cli_b):
        script = Path(cli["bin"])
        script.write_text(script.read_text().replace(
            "import json, sys, time",
            "import json, sys, time\n"
            f"with open({str(marker)!r}, 'a') as f: f.write('run\\n')"))
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "pa": budget_mod.Budget("pa", known=True, headroom=1.0),
        "pb": budget_mod.Budget("pb", known=True, headroom=1.0)})
    r = h.make_runner(
        tmp_path / "project", monkeypatch,
        providers={"pa": cli_a, "pb": cli_b},
        agents={"w_a": AgentSpec.from_dict(
                    "w_a", {"provider": "pa", "model": "ma"}),
                "w_b": AgentSpec.from_dict(
                    "w_b", {"provider": "pb", "model": "mb"})},
        project={"limits": {"provider_failure_threshold": 100}})
    return r, marker


def _trip_pa(r):
    """`pa` tripped, its cooldown already run out — half-open, so the next
    start routed onto it is the one trial."""
    tree = r.tree
    assert tree.note_run_outcome("pa", ok=False, threshold=1, kind="failed",
                                 reason="the provider stopped") is not None
    tree.set_cooldown("pa", time.time() - 1, "expired")
    assert tree.provider_health()["pa"].get("tripped"), tree.provider_health()
    assert tree.cooldown("pa") is None, "the cooldown is still running"


def _start(r, agent, **kwargs):
    async def go():
        try:
            result = await r.start(agent, "work", **kwargs)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def test_a_start_elsewhere_spends_no_trial_of_a_tripped_provider(
        tmp_path, monkeypatch):
    """`pa` is tripped with a lapsed cooldown; a real start is routed to
    healthy `pb`. `pa`'s trial must be untouched — and an admission probe
    pinned to `pa` afterwards must still be admitted."""
    r, marker = _world(tmp_path, monkeypatch)
    _trip_pa(r)

    result = _start(r, "w_b")

    assert isinstance(result, dict), result
    assert result.get("agent_id"), f"the healthy start was refused: {result}"
    assert result.get("provider") == "pb", result
    assert _launches(marker) == 1, "more than one run, or none"
    assert r.tree.provider_health().get("pa", {}).get("trial_at") is None, (
        "the start on `pb` spent `pa`'s one trial without launching there")

    probe = LaunchContext(caller=None, run_parent=None, depth=1, node_id="",
                          attempt_id="", admission_only=True)
    probed = _start(r, "w_a", model="ma", launch_context=probe)

    assert isinstance(probed, dict), probed
    assert probed.get("admitted") is True, (
        f"the pin was refused although its trial was never spent: {probed}")
    assert _launches(marker) == 1, "an admission probe launched something"


def test_two_starts_racing_for_the_lapsed_provider_yield_one_trial(
        tmp_path, monkeypatch):
    """Exactly one trial at a time per provider still holds: two starts
    routed to the same lapsed provider get one trial between them."""
    r, marker = _world(tmp_path, monkeypatch, delay=1.0)
    _trip_pa(r)

    async def both():
        first = await r.start("w_a", "work", model="ma")
        second = await r.start("w_a", "work", model="ma")
        await asyncio.wait_for(r.runs[first["agent_id"]].done.wait(), 15)
        return first, second

    first, second = asyncio.run(both())

    assert first.get("agent_id"), f"the first start never launched: {first}"
    assert first.get("provider") == "pa", first
    assert not second.get("agent_id"), f"a second trial was launched: {second}"
    assert second.get("reason"), f"the second start was refused without saying why: {second}"
    assert _launches(marker) == 1, "the provider ran twice inside one trial window"
