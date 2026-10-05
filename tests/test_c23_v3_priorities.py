"""Black-box contract for C23 amendment v3 (QH-R23..R29): priority list and
migration back up. context/specs/c23-quota-handover.md, section "Amendment v3".

Reuses the fake CLI, World, clock and budget seams of test_c23_quota_handover
(notes in tests/C23_TEST_RESULTS.md). Additions to the seams, all inert unless
a test sets them: plan keys `tool_gate` (the CLI starts a tool call, waits for
the file, writes `<file>.finished`, then ends the tool call) and `say`.

Conventions assumed (the contract leaves them open; see "Contract gaps"):
- priorities entries are mappings `{family: X}` or `{instance: X}`, with an
  optional `model`;
- a rank is the 0- or 1-based position in the expanded list, so only
  differences between ranks are asserted;
- there is no public "run the promotion check" call. A test advances the
  injected clock and calls the public resume_deferred / wait_for_agents, which
  is how the existing suite reconsiders time-dependent state. A budget refresh
  is a reading whose `read_at` is newer.
Config rejection uses ValueError, as in the v1/v2 suite.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
import yaml

import test_c23_quota_handover as base
from test_c23_quota_handover import TASK, floor_world, reserve_tool, switched, wait_until, world  # noqa: F401
from multiagents import config, server
from multiagents.budget import Budget
from multiagents.runner import Runner

PROMOTE_FIELDS = {'kind', 'agent_id', 'session_id', 'from', 'to', 'segment', 'attempt',
                  'reason', 'at', 'from_rank', 'to_rank'}


def fam(name, **kw): return {'family': name, **kw}
def inst(name, **kw): return {'instance': name, **kw}


def reading(name, headroom, read_at=None):
    return Budget(name, known=True, headroom=headroom, read_at=read_at,
                  windows={'short': {'percent': round(100 * (1 - headroom), 3), 'span_minutes': 300}})


def regain(w, name='alpha', headroom=.8):
    """The instance gets its quota back and the budget layer refreshes. QH-R25
    note 5: a refresh is a reading with a read_at strictly later than the stop,
    so the reading is stamped a second after the frozen stop time. The clock
    itself is not moved: dwell tests count from the handover."""
    w.readings[name] = reading(name, headroom, read_at=w.clock[0] + 1)
    w.set_plan(name, quota=False)


async def tick(w, seconds=0):
    w.clock[0] += seconds
    await w.r.resume_deferred()
    await server.wait_for_agents(None, 0)
    await asyncio.sleep(.25)


def instances(w): return [c['instance'] for c in w.calls()]


def promos(w, kind=None):
    rows = [json.loads(x) for x in w.paths.events_file.read_text().splitlines()] if w.paths.events_file.exists() else []
    return [e for e in rows if e.get('kind', '').startswith('promote_') and kind in (None, e['kind'])]


async def demoted(w, **beta_plan):
    """A run that quota-stops on alpha and is live on beta (rank below alpha)."""
    w.set_plan('beta', gate=str(w.tmp / 'hold-beta'), **beta_plan)
    result = await w.r.start('worker', TASK)
    await observe(lambda: len(w.calls()) >= 2, 'QH-R5 precondition: quota stop on alpha never handed over to beta')
    assert instances(w)[:2] == ['alpha', 'beta'], 'QH-R5 handover precondition'
    w.unusable('alpha')                 # alpha stays exhausted until a test regains it
    if 'tool_gate' in beta_plan: await asyncio.sleep(.5)     # let the tool call start streaming
    return result['agent_id']


async def observe(predicate, message):
    try: await wait_until(predicate)
    except pytest.fail.Exception: pytest.fail(message)


def release(w):
    for f in ('hold-alpha', 'hold-beta', 'hold-other', 'hold-reserve',
              'tool-beta', 'tool-other', 'tool-reserve'): (w.tmp / f).touch()


def promoted_to_alpha(w): return lambda: 'alpha' in instances(w)[2:]


async def await_promotion(w):
    await observe(promoted_to_alpha(w), 'QH-R25/R26: the demoted run was never migrated back to alpha')


def run_scenario(w, body):
    async def main():
        try: await body()
        finally: release(w)
    asyncio.run(main())


# ---------------------------------------------------------------- QH-R23

@pytest.mark.parametrize('entry,name', [(inst('nonesuch'), 'nonesuch'), (fam('nofamily'), 'nofamily')])
def test_qh_r23_unknown_family_or_instance_refused_at_load(world, entry, name):
    w = world()
    w.agent['priorities'] = [inst('alpha'), entry]
    w.save()
    with pytest.raises(ValueError, match=name): config.load(w.paths, seed=False)


@pytest.mark.parametrize('entries,dup', [
    ([inst('alpha'), inst('beta'), inst('beta')], 'beta'),
    ([fam('fixture'), inst('beta')], 'beta'),          # duplicate only after expansion
    ([inst('alpha'), fam('fixture')], 'alpha')])
def test_qh_r23_same_instance_twice_after_expansion_refused(world, entries, dup):
    w = world()
    w.agent['priorities'] = entries
    w.save()
    with pytest.raises(ValueError, match=dup): config.load(w.paths, seed=False)


def test_qh_r23_valid_priorities_load_with_and_without_model(world):
    w = world(agent_extra={'priorities': [inst('alpha'), inst('beta', model='model-b'), fam('different')]})
    config.load(w.paths, seed=False)


def test_qh_r23_priorities_order_decides_new_run_and_entry_model_is_used(world):
    w = world(agent_extra={'priorities': [inst('other', model='entry-model'), inst('alpha')]})
    w.set_plan('other', quota=False)
    asyncio.run(w.start())
    first = w.calls()[0]
    assert first['instance'] == 'other' and first['model'] == 'entry-model'


def test_qh_r23_entry_without_model_uses_the_agents_model_for_that_provider(world):
    w = world(models={'beta': 'model-for-beta'}, agent_extra={'priorities': [inst('beta'), inst('alpha')]})
    w.set_plan('beta', quota=False)
    asyncio.run(w.start())
    assert w.calls()[0]['instance'] == 'beta' and w.calls()[0]['model'] == 'model-for-beta'


def set_reset(w, name, seconds):
    w.readings[name] = Budget(name, known=True, headroom=.8, windows={'short': {
        'percent': 20, 'span_minutes': 300,
        'resets_at': datetime.fromtimestamp(w.clock[0] + seconds, timezone.utc).isoformat()}})


@pytest.mark.parametrize('blocked,expected', [((), 'beta'), (('beta',), 'alpha'), (('alpha', 'beta'), 'reserve')])
def test_qh_r23_family_entry_expands_by_strategy_then_reserved_last(world, blocked, expected):
    w = world(reserved='reserve', agent_extra={'priorities': [fam('fixture')]})
    w.project['budget']['instance_strategy'] = 'soonest_reset'
    w.save()
    w.reload()
    # reserve would win on soonest reset and beta beats alpha: only expansion order keeps reserve last
    set_reset(w, 'reserve', 100); set_reset(w, 'beta', 1000); set_reset(w, 'alpha', 5000)
    w.set_plan('alpha', quota=False)
    w.unusable(*blocked)
    asyncio.run(w.start())
    assert w.calls()[0]['instance'] == expected


def test_qh_r23_reserved_entry_in_family_still_obeys_the_floor(world):
    w = world(reserved='reserve', agent_extra={'priorities': [fam('fixture')]})
    w.unusable('alpha', 'beta')
    w.readings['reserve'] = reading('reserve', .2)
    asyncio.run(w.start())
    assert not w.calls() and w.events('reserve_request')


def test_qh_r5_qh_r23_quota_stop_successor_follows_priority_order_not_models_order(world):
    # all modes none: both beta (sibling) and other need a continuation run
    w = world(mode='none', models={'beta': 'model-a', 'other': 'model-o'},
              agent_extra={'priorities': [inst('alpha'), inst('other'), inst('beta')]})
    w.unusable('reserve')
    asyncio.run(w.start())
    assert instances(w)[:2] == ['alpha', 'other']


# ---------------------------------------------------------------- QH-R24 / R28 rank & pinned output

def test_qh_r24_qh_r28_rank_is_position_in_expanded_list_and_shown(world):
    w = world(agent_extra={'priorities': [inst('alpha'), inst('beta'), inst('other')]})
    w.set_plan('alpha', quota=False)
    ranks = {}
    async def scenario():
        start = await w.start()
        aid = start['agent_id']
        ranks['alpha'] = server.check_agent(aid).get('rank')
        for target in ('beta', 'other'):
            await w.steer(aid, target)
            shown = server.check_agent(aid)
            assert shown['current_provider'] == target
            ranks[target] = shown.get('rank')
            assert server.collect_agent(aid).get('rank') == ranks[target]
    asyncio.run(scenario())
    assert all(isinstance(v, int) for v in ranks.values()), ranks
    assert ranks['beta'] - ranks['alpha'] == 1 and ranks['other'] - ranks['alpha'] == 2


def test_qh_r28_agent_tree_shows_rank_and_pinned(world):
    w = world()
    asyncio.run(w.start())
    shown = json.dumps(server.agent_tree())
    assert '"rank"' in shown and '"pinned"' in shown


def test_qh_r28_pinned_is_false_by_default_and_set_by_an_explicit_provider_steer(world):
    w = world()
    w.set_plan('alpha', quota=False)
    async def scenario():
        aid = (await w.start())['agent_id']
        assert server.check_agent(aid).get('pinned') in (False, None, '')
        assert 'pinned' in server.check_agent(aid)
        await w.steer(aid, 'beta')
        assert server.check_agent(aid)['pinned']
        assert server.collect_agent(aid)['pinned']
    asyncio.run(scenario())


# ---------------------------------------------------------------- QH-R25 trigger

def test_qh_r25_quota_reset_promotes_run_back_to_best_instance(world):
    w = world()
    async def body():
        aid = await demoted(w)
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
        a, b, c = w.calls()[:3]
        assert c['resume'] == a['session_id'] == b['session_id']          # QH-R8 resume
        assert (c['cwd'], c['branch'], c['model']) == (a['cwd'], a['branch'], a['model'])
        check = server.check_agent(aid)
        assert check['agent_id'] == aid and check['current_provider'] == 'alpha'
        assert check['home_provider'] == 'alpha'
    run_scenario(w, body)


def test_qh_r25_no_promotion_while_no_better_instance_is_usable(world):
    w = world()
    async def body():
        await demoted(w)
        await tick(w, 600)
        await tick(w, 600)
        assert instances(w) == ['alpha', 'beta'] and not promos(w)
    run_scenario(w, body)


@pytest.mark.parametrize('headroom,promotes', [(.05, False), (.099, False), (.10, True), (.5, True)])
def test_qh_r25_hysteresis_target_needs_min_headroom(world, headroom, promotes):
    w = world()
    async def body():
        await demoted(w)
        regain(w, headroom=headroom)
        await tick(w, 300)
        await asyncio.sleep(.3)
        assert ('alpha' in instances(w)[2:]) == promotes
        assert bool(promos(w, 'promote_started')) == promotes
    run_scenario(w, body)


def test_qh_r25_min_dwell_is_measured_on_the_current_instance(world):
    w = world()
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 299)
        assert instances(w) == ['alpha', 'beta'] and not promos(w), 'dwell 299 < 300'
        await tick(w, 1)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r25_promotes_to_the_best_improved_instance_not_the_next_one(world):
    w = world(models={'beta': 'model-a', 'other': 'model-o'},
              agent_extra={'priorities': [inst('alpha'), inst('beta'), inst('other')]})
    w.unusable('reserve')
    w.set_plan('beta', quota=True)
    w.set_plan('other', gate=str(w.tmp / 'hold-other'))
    async def body():
        await w.r.start('worker', TASK)
        await wait_until(lambda: len(w.calls()) >= 3)
        assert instances(w)[:3] == ['alpha', 'beta', 'other']
        w.unusable('alpha', 'beta')
        regain(w, 'beta'); regain(w, 'alpha')
        await tick(w, 300)
        await wait_until(lambda: len(w.calls()) >= 4)
        assert instances(w)[3] == 'alpha'
        done = promos(w, 'promote_started')
        assert len(done) == 1 and done[0]['to'] == 'alpha'
    run_scenario(w, body)


def test_qh_r25_promotion_from_beta_to_the_better_of_two_improved_when_alpha_stays_down(world):
    w = world(models={'beta': 'model-a', 'other': 'model-o'},
              agent_extra={'priorities': [inst('alpha'), inst('beta'), inst('other')]})
    w.unusable('reserve')
    w.set_plan('beta', quota=True)
    w.set_plan('other', gate=str(w.tmp / 'hold-other'))
    async def body():
        await w.r.start('worker', TASK)
        await wait_until(lambda: len(w.calls()) >= 3)
        w.unusable('alpha', 'beta')
        regain(w, 'beta')
        await tick(w, 300)
        await wait_until(lambda: len(w.calls()) >= 4)
        assert instances(w)[3] == 'beta'
    run_scenario(w, body)


def test_qh_r25_run_already_on_best_usable_instance_is_not_touched(world):
    w = world()
    w.set_plan('alpha', quota=False, gate=str(w.tmp / 'hold-alpha'))
    async def body():
        await w.r.start('worker', TASK)
        await wait_until(lambda: bool(w.calls()))
        w.readings['beta'] = reading('beta', .9, read_at=w.clock[0])
        await tick(w, 900)
        assert instances(w) == ['alpha'] and not promos(w)
    run_scenario(w, body)


def test_qh_r25_qh_r17_floor_run_is_promoted_off_the_floor_when_better_instance_returns(world):
    w = floor_world(world)
    w.set_plan('reserve', gate=str(w.tmp / 'hold-reserve'))
    async def body():
        await w.r.start('worker', TASK)
        requests = w.events('reserve_request')
        assert requests
        await reserve_tool('allow_reserve', requests[0]['request_id'])
        await w.r.resume_deferred()
        await wait_until(lambda: bool(w.calls()))
        assert instances(w) == ['reserve']
        regain(w)
        await tick(w, 300)
        await wait_until(lambda: instances(w)[-1] == 'alpha')
    run_scenario(w, body)


# ---------------------------------------------------------------- QH-R26 pause and migration

def test_qh_r26_pause_waits_for_end_of_tool_call_in_progress(world):
    w = world()
    tool = w.tmp / 'tool-beta'
    async def body():
        await demoted(w, tool_gate=str(tool))
        regain(w)
        await tick(w, 300)
        await tick(w, 10)
        assert instances(w) == ['alpha', 'beta'], 'tool call in progress: not stopped yet'
        assert not (w.tmp / 'tool-beta.finished').exists()
        tool.touch()
        await await_promotion(w)
        assert (w.tmp / 'tool-beta.finished').exists(), 'stopped after the tool call ended'
    run_scenario(w, body)


def test_qh_r26_stop_is_forced_after_grace_while_tool_call_still_runs(world):
    w = world()
    async def body():
        await demoted(w, tool_gate=str(w.tmp / 'tool-beta'))
        regain(w)
        await tick(w, 300)                      # trigger
        await tick(w, 119)
        assert instances(w) == ['alpha', 'beta'], 'inside the 120 s grace'
        await tick(w, 1)
        await await_promotion(w)
        assert not (w.tmp / 'tool-beta.finished').exists(), 'tool call never ended: stop was forced'
    run_scenario(w, body)


def test_qh_r26_turn_with_no_tool_call_is_stopped_at_once(world):
    w = world()
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r26_migration_prompt_says_priority_not_error(world):
    w = world()
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
        prompt = w.calls()[2]['prompt'].lower()
        assert 'priorit' in prompt
    run_scenario(w, body)


def test_qh_r26_refused_target_falls_back_to_previous_instance_and_is_not_retried(world):
    w = world()
    w.set_plan('alpha', reject=True)
    async def body():
        aid = await demoted(w)
        regain(w)
        w.set_plan('alpha', quota=False, reject=True)
        await tick(w, 300)
        await wait_until(lambda: len(w.calls()) >= 4)
        assert instances(w)[2:4] == ['alpha', 'beta'], w.calls()
        assert w.calls()[3]['resume'] == w.calls()[0]['session_id'], 'resumes the same session on beta'
        failed = promos(w, 'promote_failed')
        assert len(failed) == 1 and not promos(w, 'promote_completed')
        check = server.check_agent(aid)
        assert check['current_provider'] == 'beta' and check['agent_id'] == aid
        # no refresh since the failure: not retried however long it takes
        for _ in range(3): await tick(w, 600)
        assert instances(w).count('alpha') == 2, 'alpha was tried once as a promotion target'
        # next budget refresh of that instance: the retry is allowed (and now succeeds)
        w.set_plan('alpha', reject=False)
        regain(w)
        await tick(w, 1)
        await wait_until(lambda: instances(w)[-1] == 'alpha')
    run_scenario(w, body)


@pytest.mark.parametrize('mode,changed,resumes', [('copy', False, True), ('copy', True, False), ('none', False, False)])
def test_qh_r26_cross_family_back_resumes_still_resumable_session_else_continuation(world, mode, changed, resumes):
    w = world(mode=mode, models={'other': 'model-o'})
    w.unusable('beta', 'reserve')
    w.set_plan('other', gate=str(w.tmp / 'hold-other'))
    async def body():
        aid = (await w.r.start('worker', TASK))['agent_id']
        await wait_until(lambda: len(w.calls()) >= 2)
        a, b = w.calls()[:2]
        assert b['instance'] == 'other' and b['session_id'] != a['session_id']
        w.unusable('alpha')
        if changed:
            source = w.stores['alpha'] / (a['session_id'] + '.jsonl')
            source.write_text(source.read_text() + '{"external":"changed home transcript"}\n')
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
        c = w.calls()[2]
        if resumes: assert c['resume'] == a['session_id']
        else:
            assert not c['resume'] and TASK in c['prompt']
            assert f'.multiagents/runs/{aid}' in c['prompt']
        assert 'priorit' in c['prompt'].lower()
        assert (c['cwd'], c['branch']) == (a['cwd'], a['branch'])
        assert server.check_agent(aid)['agent_id'] == aid
    run_scenario(w, body)


# ---------------------------------------------------------------- QH-R27 exceptions

def test_qh_r27_no_promotion_while_awaiting_user(world):
    w = world()
    w.set_plan('beta', say='NEED_DECISION(topic): which way should this go?\nDEFAULT: left')
    async def body():
        aid = (await w.r.start('worker', TASK))['agent_id']
        await wait_until(lambda: len(w.calls()) >= 2)
        w.unusable('alpha')
        await w.settle(aid)
        assert w.r.check(aid)['status'] == 'awaiting_user'
        regain(w)
        await tick(w, 900)
        await tick(w, 900)
        assert instances(w) == ['alpha', 'beta'] and not promos(w)
        assert w.r.check(aid)['status'] == 'awaiting_user'
    run_scenario(w, body)


async def hold_on(w, target, gate_name):
    """Live run, explicitly pinned to `target` by the orchestrator."""
    w.set_plan(target, quota=False, gate=str(w.tmp / gate_name))
    aid = (await w.r.start('worker', TASK))['agent_id']
    await wait_until(lambda: bool(w.calls()))
    return aid


async def start_live_pinnable(w, tool_name):
    """Live run on alpha whose CLI has emitted its session id (a steer needs it).

    The fake CLI only prints a session id with its first stream event, so the
    hold is a `tool_gate`, which prints one before it waits, not a `gate`.
    """
    w.set_plan('alpha', quota=False, tool_gate=str(w.tmp / tool_name))
    aid = (await w.r.start('worker', TASK))['agent_id']
    def session_captured():
        rows = [json.loads(x) for x in w.paths.events_file.read_text().splitlines()] if w.paths.events_file.exists() else []
        return any(e.get('kind') == 'session' and aid in (e.get('agent'), e.get('agent_id')) for e in rows)
    await wait_until(session_captured)
    return aid


def test_qh_r27_explicitly_pinned_run_is_not_promoted_and_new_runs_still_launch_on_best(world):
    w = world()
    async def body():
        aid = await start_live_pinnable(w, 'tool-alpha')
        w.set_plan('beta', tool_gate=str(w.tmp / 'tool-beta'))
        result = await server.steer_agent(aid, 'Continue assigned work', provider='beta')
        assert not result.get('error'), result
        await wait_until(lambda: instances(w)[-1] == 'beta')
        assert server.check_agent(aid)['pinned']
        before = len(w.calls())
        await tick(w, 900); await tick(w, 900)
        assert len(w.calls()) == before and not promos(w), 'pinned to beta although alpha is usable'
        # new runs are not affected by anybody's pin
        second = await w.r.start('worker', 'second task')
        await wait_until(lambda: len(w.calls()) > before)
        assert w.calls()[before]['instance'] == 'alpha' and second['agent_id'] != aid
    run_scenario(w, body)


def test_qh_r27_steer_provider_auto_unpins_and_promotion_resumes(world):
    w = world()
    async def body():
        aid = await start_live_pinnable(w, 'tool-alpha')
        w.set_plan('beta', tool_gate=str(w.tmp / 'tool-beta'))
        await server.steer_agent(aid, 'go to beta', provider='beta')
        await wait_until(lambda: instances(w)[-1] == 'beta')
        await tick(w, 900)
        assert instances(w)[-1] == 'beta'
        result = await server.steer_agent(aid, 'back to automatic placement', provider='auto')
        assert not result.get('error'), result
        await asyncio.sleep(.3)
        assert not server.check_agent(aid)['pinned']
        # wherever the auto steer put the run, it ends up on the best instance
        await tick(w, 900)
        await tick(w, 900)
        await wait_until(lambda: instances(w)[-1] == 'alpha')
        assert server.check_agent(aid)['current_provider'] == 'alpha'
    run_scenario(w, body)


# ---------------------------------------------------------------- QH-R28 events

def test_qh_r28_promote_events_carry_ranks_and_contract_fields(world):
    w = world(agent_extra={'priorities': [inst('alpha'), inst('beta')]})
    async def body():
        aid = await demoted(w)
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
        await wait_until(lambda: bool(promos(w, 'promote_completed')))
        kinds = [e['kind'] for e in promos(w)]
        assert kinds == ['promote_started', 'promote_completed'], kinds
        for e in promos(w):
            assert PROMOTE_FIELDS <= e.keys(), e
            assert e['agent_id'] == aid and e['from'] == 'beta' and e['to'] == 'alpha'
            assert e['from_rank'] - e['to_rank'] == 1 and e['to_rank'] < e['from_rank']
            assert e['session_id'] == w.calls()[0]['session_id']
    run_scenario(w, body)


def test_qh_r28_promote_failed_event_has_ranks_and_reason(world):
    w = world()
    async def body():
        await demoted(w)
        w.set_plan('alpha', quota=False, reject=True)
        regain(w); w.set_plan('alpha', quota=False, reject=True)
        await tick(w, 300)
        await wait_until(lambda: bool(promos(w, 'promote_failed')))
        e = promos(w, 'promote_failed')[0]
        assert PROMOTE_FIELDS <= e.keys() and e['reason'] and e['from_rank'] > e['to_rank']
        assert [x['kind'] for x in promos(w)][0] == 'promote_started'
    run_scenario(w, body)


# ---------------------------------------------------------------- QH-R29 settings

KEYS_OK = [('promote_min_headroom', 0), ('promote_min_headroom', 0.0), ('promote_min_headroom', .999),
           ('promote_min_dwell_seconds', 0), ('promote_grace_seconds', 0),
           ('promote_check_seconds', 1), ('promote_min_dwell_seconds', 86400)]
KEYS_BAD = [('promote_min_headroom', 1), ('promote_min_headroom', -.01), ('promote_min_headroom', 1.5),
            ('promote_min_headroom', float('nan')), ('promote_min_headroom', 'lots'),
            ('promote_min_dwell_seconds', -1), ('promote_min_dwell_seconds', 1.5),
            ('promote_min_dwell_seconds', 'soon'), ('promote_grace_seconds', -1),
            ('promote_grace_seconds', 2.5), ('promote_check_seconds', 0),
            ('promote_check_seconds', -60), ('promote_check_seconds', 1.5)]


@pytest.mark.parametrize('key,value', KEYS_OK)
def test_qh_r29_valid_project_values_load(world, key, value):
    w = world(options={key: value})
    config.load(w.paths, seed=False)


@pytest.mark.parametrize('key,value', KEYS_BAD)
def test_qh_r29_invalid_project_values_refused_naming_the_key(world, key, value):
    w = world(options={})
    w.project['quota_handover'][key] = value
    w.save()
    with pytest.raises(ValueError, match=key): config.load(w.paths, seed=False)


@pytest.mark.parametrize('key,value', KEYS_OK[:6])
def test_qh_r29_valid_per_agent_values_load(world, key, value):
    w = world(agent_extra={'quota_handover': {key: value}})
    config.load(w.paths, seed=False)


@pytest.mark.parametrize('key,value', KEYS_BAD)
def test_qh_r29_invalid_per_agent_values_refused_naming_the_key(world, key, value):
    with pytest.raises(ValueError, match=key): world(agent_extra={'quota_handover': {key: value}})


def test_qh_r29_dwell_zero_means_immediate(world):
    w = world(options={'promote_min_dwell_seconds': 0})
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 0)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_nondefault_dwell_is_honoured(world):
    w = world(options={'promote_min_dwell_seconds': 30})
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 29)
        assert instances(w) == ['alpha', 'beta']
        await tick(w, 1)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_nondefault_headroom_is_honoured(world):
    w = world(options={'promote_min_headroom': .5})
    async def body():
        await demoted(w)
        regain(w, headroom=.49)
        await tick(w, 300)
        assert instances(w) == ['alpha', 'beta']
        regain(w, headroom=.5)
        await tick(w, 1)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_headroom_zero_accepts_any_usable_target(world):
    w = world(options={'promote_min_headroom': 0})
    async def body():
        await demoted(w)
        regain(w, headroom=.03)   # above the existing admission reserve (<= .02 is not usable)
        await tick(w, 300)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_grace_zero_stops_a_running_tool_call_immediately(world):
    w = world(options={'promote_grace_seconds': 0})
    async def body():
        await demoted(w, tool_gate=str(w.tmp / 'tool-beta'))
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
        assert not (w.tmp / 'tool-beta.finished').exists()
    run_scenario(w, body)


def test_qh_r29_nondefault_grace_is_honoured(world):
    w = world(options={'promote_grace_seconds': 30})
    async def body():
        await demoted(w, tool_gate=str(w.tmp / 'tool-beta'))
        regain(w)
        await tick(w, 300)
        await tick(w, 29)
        assert instances(w) == ['alpha', 'beta']
        await tick(w, 1)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_per_agent_override_wins_over_project_value(world):
    w = world(options={'promote_min_dwell_seconds': 0},
              agent_extra={'quota_handover': {'promote_min_dwell_seconds': 1000}})
    async def body():
        await demoted(w)
        regain(w)
        await tick(w, 999)
        assert instances(w) == ['alpha', 'beta']
        await tick(w, 1)
        await await_promotion(w)
    run_scenario(w, body)


def test_qh_r29_per_agent_override_does_not_leak_to_other_agents(world):
    w = world(agent_extra={'quota_handover': {'promote_min_dwell_seconds': 100000}})
    agents = yaml.safe_load((w.paths.config / 'agents.yaml').read_text())
    agents['agents']['plain'] = {k: v for k, v in agents['agents']['worker'].items() if k != 'quota_handover'}
    (w.paths.config / 'agents.yaml').write_text(yaml.safe_dump(agents))
    w.r = Runner(w.paths, config.load(w.paths, seed=False))
    w.set_plan('beta', gate=str(w.tmp / 'hold-beta'))
    async def body():
        await w.r.start('plain', TASK)
        await wait_until(lambda: len(w.calls()) >= 2)
        w.unusable('alpha')
        regain(w)
        await tick(w, 300)
        await await_promotion(w)
    run_scenario(w, body)
