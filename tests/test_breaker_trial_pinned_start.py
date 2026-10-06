"""bug-521be6: a model-pinned start must not spend the breaker's trial in
pre-admission.

A tripped provider whose cooldown has lapsed is allowed exactly one trial
(`Tree.claim_trial`, tree.py). `_pin_health()` runs before admission is
decided and called `_half_open()` to make its decision — which CLAIMED that
trial and then launched nothing. The routing pass in the same `start()`
found its own claim held by "somebody else", cooled the provider for a
minute, and refused the pin that had just asked for it. So every pinned
attempt burned a trial without running anything, the provider stayed refused
until the window aged out, and unpinned work went to fallbacks meanwhile.

So the rule is narrower than "never claim in a check": it is that a trial is
claimed only by the code path that is about to launch, and only after every
check that could still refuse. `_pin_health` therefore only ever observes — it
reads the breaker's state and refuses what it must. `start()`'s routing pass
claims, because it is what decides where the run goes and the claim is what
routes a barrage away. A steer has no routing pass, so it claims separately,
in `_claim_trial`, once nothing is left that could refuse.
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
from multiagents.runner import LaunchContext, Runner  # noqa: E402


def _launches(marker: Path) -> int:
    """How many times the fake CLI has actually been spawned."""
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def _world(tmp_path, monkeypatch, *, delay=0.0, resumable=False, fails_on=""):
    """One fake provider whose every spawn appends a line to `marker`.

    The CLI is a real executable standing in for the agent's CLI (the seam
    `c3_harness` draws); the marker is how "exactly one run" becomes a fact
    about the filesystem rather than about a returned dict. `resumable` gives
    it a session id and a resume argv, which a steer needs.

    `fails_on` names a file whose mere existence makes every LATER spawn exit
    non-zero. It is read by the process at run time rather than baked into the
    script, because a rewrite of the script cannot reach a spawn that has
    already read it: `steer()` returns after the relaunch has started, which on
    a fast host is after it has also finished. A test that needs its run to
    fail creates the file, and the next spawn it starts fails for certain.
    """
    marker = tmp_path / "launches"
    events = [{"type": "text", "text": "ok"}]
    if resumable:
        events[0]["session_id"] = "s-1"
    cli = h.fake_cli(tmp_path, "p", events=events, delay=delay)
    if resumable:
        cli["stream"]["session_id_paths"] = ["session_id"]
        cli["spawn"]["resume"] = ["--resume", "{session_id}"]
    script = Path(cli["bin"])
    head = ("import json, os, sys, time\n"
            f"with open({str(marker)!r}, 'a') as f: f.write('run\\n')")
    if fails_on:
        head += f"\nEXIT = 1 if os.path.exists({str(fails_on)!r}) else 0"
    text = script.read_text().replace("import json, sys, time", head)
    if fails_on:
        text = text.replace("sys.exit(0)", "sys.exit(EXIT)")
    script.write_text(text)
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "p": budget_mod.Budget("p", known=True, headroom=1.0)})   # usable quota
    r = h.make_runner(
        tmp_path / "project", monkeypatch, providers={"p": cli},
        agents={"worker": AgentSpec.from_dict(
            "worker", {"provider": "p", "model": "m1"})},
        project={"limits": {"provider_failure_threshold": 100}})
    return r, marker


def _lapsed(r):
    """`provider_health` tripped, its cooldown already run out — half-open, so
    the next start is the one trial."""
    tree = r.tree
    assert tree.note_run_outcome("p", ok=False, threshold=1, kind="failed",
                                 reason="the provider stopped") is not None
    tree.set_cooldown("p", time.time() - 1, "expired")
    assert tree.provider_health()["p"].get("tripped"), tree.provider_health()
    assert tree.cooldown("p") is None, "the cooldown is still running"


def _start(r, **kwargs):
    async def go():
        try:
            result = await r.start("worker", "work", **kwargs)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _steer(r, agent_id, message, settle=False):
    """`Runner.steer`, on a Runner of its own and a loop of its own.

    `settle` holds that loop open until the relaunched run has finished. A
    Runner records a run's outcome from the loop its `_consume` is running on,
    so a loop that closes the instant `steer()` returns leaves the relaunch
    uncounted and the breaker untouched — and what this file is about is what
    the relaunch left in the breaker. Waiting on `run.done` says so outright,
    where sleeping for a guessed interval would only usually say so.
    """
    async def go():
        result = await r.steer(agent_id, message)
        run = r.runs.get(agent_id)
        if settle and run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def test_a_pinned_start_runs_the_trial_it_was_admitted_for(tmp_path, monkeypatch):
    """The pin names the provider, its quota is good and its cooldown has
    lapsed: exactly one run goes out, on `p`. Pre-admission checking the
    breaker must not be what spends the trial — the launch is."""
    r, marker = _world(tmp_path, monkeypatch)
    _lapsed(r)

    result = _start(r, model="m1")

    assert isinstance(result, dict), result
    assert result.get("agent_id"), (
        f"the pinned start was refused instead of launching: {result}")
    assert result.get("provider") == "p", result
    assert _launches(marker) == 1, "more than one run, or none"


def test_a_second_pinned_start_inside_the_trial_window_launches_nothing(
        tmp_path, monkeypatch):
    """One trial at a time is still one trial at a time. While the pinned
    start's run is in flight the claim is held, so a second pinned start is
    refused rather than spending the provider's only retry twice."""
    r, marker = _world(tmp_path, monkeypatch, delay=1.0)
    _lapsed(r)

    async def both():
        first = await r.start("worker", "work", model="m1")
        second = await r.start("worker", "work", model="m1")
        await asyncio.wait_for(r.runs[first["agent_id"]].done.wait(), 15)
        return first, second

    first, second = asyncio.run(both())

    assert first.get("agent_id"), f"the first pinned start never launched: {first}"
    assert not second.get("agent_id"), f"a second trial was launched: {second}"
    assert second.get("reason"), f"the second start was refused without saying why: {second}"
    assert _launches(marker) == 1, "the provider ran twice inside one trial window"


def test_an_unpinned_start_inside_the_trial_window_is_unchanged(tmp_path, monkeypatch):
    """The trial is the router's, not `_pin_health`'s: an unpinned start made
    while it is held is routed around and deferred, which is what it did
    before and must keep doing."""
    r, marker = _world(tmp_path, monkeypatch, delay=1.0)
    _lapsed(r)

    async def both():
        pinned = await r.start("worker", "work", model="m1")
        unpinned = await r.start("worker", "work")
        await asyncio.wait_for(r.runs[pinned["agent_id"]].done.wait(), 15)
        return unpinned

    unpinned = asyncio.run(both())

    assert isinstance(unpinned, dict), unpinned
    assert not unpinned.get("agent_id"), f"the tripped provider took the run: {unpinned}"
    assert unpinned.get("deferred"), unpinned
    assert _launches(marker) == 1


def test_a_pinned_steer_still_spends_the_trial(tmp_path, monkeypatch):
    """The other caller of `_pin_health`, and the reason it cannot simply
    observe: a steer goes straight to `_launch`, with no routing pass behind
    it. So it is the caller that spends the trial — a steer of a half-open
    provider is a run into it, and one trial at a time is the whole point.

    The CLI is told to fail so the steered run FAILS: a success clears
    `trial_at` (that is what a recovery is), which would erase the very record
    this is about. The lever is a file the process reads at spawn time and it
    stands BEFORE the relaunch, because a run records its outcome as soon as
    its process ends — on a fast host, before the steer has even returned, so
    an edit to the script after the fact reaches nothing (see `_world`).
    The steered run's failure also sets a fresh cooldown, which is expired
    again by hand so that what the second steer meets is a half-open provider
    whose trial is held — not one that is simply cooling.
    """
    flag = tmp_path / "fail"
    r, _ = _world(tmp_path, monkeypatch, resumable=True, fails_on=flag)
    first = _start(r, model="m1")
    assert first.get("agent_id"), first
    node = first["agent_id"]
    _lapsed(r)

    flag.write_text("every spawn from here on fails\n")
    steered = _steer(Runner(r.paths, r.config), node, "more", settle=True)
    assert steered.get("steered") is True, (
        f"the first pinned steer may take the trial: {steered}")

    r.tree.set_cooldown("p", time.time() - 1, "expired")
    assert r.tree.provider_health()["p"].get("trial_at"), (
        "the steered run must leave the trial it claimed standing")

    refused = _steer(Runner(r.paths, r.config), node, "and again")

    assert refused.get("steered") is False, (
        f"a second steer ran into a half-open provider anyway: {refused}")
    assert refused.get("reason"), (
        f"the second steer was refused without saying why: {refused}")


def test_a_planned_admission_does_not_spend_the_trial(tmp_path, monkeypatch):
    """Review finding 1: the scheduler admits a node by asking `start()` the
    question and reading the answer (`scheduler/engine.py`, `probe=True`). It
    launches nothing — the activation is spawned separately afterwards — so it
    must read the breaker, not spend its trial. Spending it there meant the
    real start that followed the probe found its own claim held, cooled the
    provider for a minute and refused the pin the probe had just admitted.

    So the probe admits, runs nothing, and the start behind it still gets the
    provider's one retry."""
    r, marker = _world(tmp_path, monkeypatch)
    _lapsed(r)

    probe = LaunchContext(caller=None, run_parent=None, depth=1, node_id="",
                          attempt_id="", admission_only=True)
    probed = _start(r, model="m1", launch_context=probe)

    assert probed.get("admitted") is True, (
        f"the pin was not admitted at all, which is a different bug: {probed}")
    assert _launches(marker) == 0, "an admission probe launched something"

    result = _start(r, model="m1")

    assert result.get("agent_id"), (
        f"the probe spent the trial, so the real start was refused: {result}")
    assert _launches(marker) == 1, "more than one run, or none"


def test_a_pinned_steer_refused_after_its_health_check_keeps_the_trial(
        tmp_path, monkeypatch):
    """Review finding 2: the claim has to come after every check that can still
    refuse, or a refusal spends the provider's one retry without running
    anything. `_model_refusal` is one such check — a roster change since the run
    started — and it sits between the health check and the relaunch.

    The steer is refused, so the trial is still free, and the next pinned steer
    may take it. Before the round-2 fix the first steer claimed it before
    `_model_refusal` ran, the refusal gave the run back untouched, and the
    retry that followed found a trial held by a claim nothing had ever
    launched against."""
    r, _ = _world(tmp_path, monkeypatch, resumable=True)
    first = _start(r, model="m1")
    assert first.get("agent_id"), first
    node = first["agent_id"]
    _lapsed(r)

    excluded = Runner(r.paths, r.config)
    excluded.providers["p"].models_exclude = ["m1"]
    refused = _steer(excluded, node, "more")
    assert refused.get("steered") is False, (
        f"the allowlist change should have refused the respawn: {refused}")
    assert "m1" in (refused.get("error") or ""), (
        f"refused for the wrong reason: {refused}")

    steered = _steer(Runner(r.paths, r.config), node, "and again")

    assert steered.get("steered") is True, (
        f"the refused steer spent the trial, so this one was refused for "
        f"nothing: {steered}")
