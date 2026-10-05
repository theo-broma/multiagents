"""Adversarial pass on C23 part 2 (QH-R23..R30): promotion back up the list.

Reuses the World fixture and helpers of the v3 contract suite.
"""
from __future__ import annotations

import asyncio

from multiagents import server
from test_c23_quota_handover import TASK, wait_until, world  # noqa: F401
from test_c23_v3_priorities import demoted, instances, promos, reading, release, run_scenario, tick


def test_adv2_stale_reading_older_than_the_quota_stop_does_not_promote(world):
    """QH-R30.5: a refresh is a *more recent* read_at. When the quota stop on
    alpha had no stamped reading, a cached reading whose read_at predates the
    stop itself is not news about alpha, yet it clears the stop and promotes
    the run straight back into an exhausted account (alpha still quota-stops),
    which then hands over to beta again: ping-pong on every dwell period."""
    w = world()
    async def body():
        stop_time = w.clock[0]
        await demoted(w)
        # A pre-exhaustion reading, read an hour before the stop; alpha is
        # in fact still exhausted (plan quota stays True).
        w.readings['alpha'] = reading('alpha', .8, read_at=stop_time - 3600)
        await tick(w, 300)
        await asyncio.sleep(.5)
        assert not promos(w, 'promote_started'), promos(w)
        assert instances(w) == ['alpha', 'beta'], instances(w)
    run_scenario(w, body)


def test_adv2_stale_reading_cannot_ping_pong_repeatedly(world):
    """Same seam, counted: with one stale reading and dwell elapsing, the run
    must not bounce alpha -> beta -> alpha more than the first handover."""
    w = world()
    async def body():
        stop_time = w.clock[0]
        await demoted(w)
        w.readings['alpha'] = reading('alpha', .8, read_at=stop_time - 3600)
        w.set_plan('beta', gate=None)
        for _ in range(4):
            await tick(w, 301)
            await asyncio.sleep(.3)
        assert instances(w).count('alpha') == 1, instances(w)
    run_scenario(w, body)


def test_adv2_explicit_pin_during_promotion_safe_point_wait_is_honoured(world):
    """QH-R27: steer_agent(..., provider=X) pins the run to X. Issued while a
    promotion waits for the tool call to end, the orchestrator's explicit pin
    is refused with 'quota handover is in progress' and the automatic
    promotion overrides it: the run lands on alpha, unpinned."""
    w = world()
    tool = w.tmp / 'tool-beta'
    async def body():
        aid = await demoted(w, tool_gate=str(tool))
        w.clock[0] += 1     # QH-R25 note 5: the refresh is strictly after the stop
        w.readings['alpha'] = reading('alpha', .8, read_at=w.clock[0])
        w.set_plan('alpha', quota=False)
        await tick(w, 300)
        await wait_until(lambda: bool(promos(w, 'promote_started')))
        result = await server.steer_agent(aid, 'stay on beta', provider='beta')
        tool.touch()
        await asyncio.sleep(1.5)
        assert not result.get('error'), result
        check = server.check_agent(aid)
        assert check['pinned'] and check['current_provider'] == 'beta', (check, instances(w))
    run_scenario(w, body)
