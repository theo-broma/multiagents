"""TF-R5: real conversational turns retry without returning partial advice."""
import asyncio
import json

import pytest

from test_tf_support import w, WAIT, failures
from multiagents.config import load
from multiagents.runner import Runner

MESSAGE = 'TF-QUESTION-447: what should we do?'
PARTIAL = 'TF-FAILED-PARTIAL-ADVICE'
ANSWER = 'TF-COMPLETE-ANSWER'


@pytest.fixture
def conversation(w, monkeypatch):
    run = build_conversation(w, monkeypatch)
    yield w, run
    teardown_conversation(w, run)


@pytest.fixture
def backing_off(w, monkeypatch):
    """A consult world whose retries back off 1.5 s inside the call's
    deadline (TF-R2's backoff, on the interface's `backoff_seconds`), so a
    second caller can be placed between two tries."""
    run = build_conversation(w, monkeypatch,
                             retries={'backoff_seconds': [1.5, 1.5, 1.5]})
    yield w, run
    teardown_conversation(w, run)


@pytest.fixture
def capped(w, monkeypatch):
    """A consult world whose provider is just under its spend cap."""
    w.fxw.entry['spend_cap'] = {'usd': 0.1}
    run = build_conversation(w, monkeypatch,
                             retries={'backoff_seconds': [0.1, 0.1, 0.1]})
    yield w, run
    teardown_conversation(w, run)


@pytest.fixture
def sole_slot(w, monkeypatch):
    """A consult world whose provider admits one run at a time, so a retry
    that re-entered admission would have to count its own dead try."""
    w.fxw.entry['max_concurrent'] = 1
    run = build_conversation(w, monkeypatch,
                             retries={'backoff_seconds': [0.1, 0.1, 0.1]})
    yield w, run
    teardown_conversation(w, run)


def build_conversation(w, monkeypatch, **scheduler):
    monkeypatch.setattr(Runner, 'CONSULT_LOCK_SLACK_SECONDS', 0)
    w.project['scheduler']['enabled'] = False
    if scheduler:
        w.project['scheduler'].update(scheduler)
    w.agent('advisor', 'fxw', conversational=True, writes=False, can_spawn=False)
    w.write_config()
    run = Runner(w.paths, load(w.paths, seed=False))
    w.gates_to_open = ['deadline']
    return run


def teardown_conversation(w, run):
    for name in getattr(w, 'gates_to_open', []):
        w.gate(name, w.fxw)
    asyncio.run(run.shutdown(detach=False))


def test_tf_r5_crash_once_then_answer_replays_message_on_same_session(conversation):
    w, run = conversation
    w.fxw.queue({'text': PARTIAL, 'exit': 1}, {'text': ANSWER})
    async def go():
        return await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=3), WAIT)
    reply = asyncio.run(go())
    assert not reply.get('error'), reply
    assert ANSWER in json.dumps(reply) and PARTIAL not in str(reply.get('reply', ''))
    assert len(w.fxw.calls()) == 2
    first, second = w.fxw.calls()
    assert MESSAGE in first['prompt'] and MESSAGE in second['prompt']
    assert second['resume'] == first['session']
    assert PARTIAL in (w.paths.run_dir(reply['agent_id']) / 'stream.jsonl').read_text()


def test_tf_r5_persistent_crash_returns_error_naming_class_and_no_partial_advice(conversation):
    w, run = conversation
    w.fxw.queue(*([{'text': PARTIAL, 'exit': 1}] * 5))
    async def go():
        return await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=3), WAIT)
    reply = asyncio.run(go())
    assert 'crash' in str(reply.get('error', '')).lower(), reply
    assert len(w.fxw.calls()) == 4, 'consult must use the same three-retry budget'
    assert PARTIAL not in str(reply.get('reply', ''))


def test_tf_r5_deadline_during_retry_is_not_extended(conversation):
    w, run = conversation
    w.fxw.queue({'exit': 1}, {'gate': 'deadline', 'text': ANSWER})
    async def go():
        loop = asyncio.get_running_loop()
        began = loop.time()
        reply = await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=1), WAIT)
        return reply, loop.time() - began
    reply, elapsed = asyncio.run(go())
    assert reply.get('error'), reply
    assert len(w.fxw.calls()) >= 2, 'the deadline must be reached during a retry'
    # Process teardown and polling may finish just after the one-second bound;
    # a second full deadline is not allowed.
    assert elapsed < 1.8, f'consult restarted its one-second deadline: {elapsed}'
    assert ANSWER not in json.dumps(reply)


def test_tf_r5_lost_session_uses_fresh_session_and_reports_it(conversation):
    w, run = conversation
    # Existing session-loss status is a public provider stream contract.
    script = w.fxw.dir / 'agent.py'
    source = script.read_text()
    source = source.replace('if fx.get("stderr"):', '''
if fx.get("lose_session"):
    emit("result", {"status": "SESSION_LOST", "text": "session not found"})
if fx.get("stderr"):''')
    script.write_text(source)
    w.fxw.entry['stream']['rules'].append({'match': {'type': 'result'}, 'as': 'result',
                                         'fields': {'status': 'part.status', 'text': 'part.text'}})
    w.write_config()
    run.reload(load(w.paths, seed=False))
    w.fxw.queue({'exit': 1}, {'lose_session': True, 'exit': 1}, {'text': ANSWER})
    async def go():
        return await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=3), WAIT)
    reply = asyncio.run(go())
    assert not reply.get('error') and ANSWER in json.dumps(reply), reply
    assert len(w.fxw.calls()) == 3
    assert w.fxw.calls()[-1]['resume'] is None
    notice = json.dumps(reply).lower().replace('_', ' ').replace('-', ' ')
    assert any(word in notice for word in ('fresh', 'new session', 'restarted', 'replaced')), reply


# ----------------------------------------------- what is kept between tries

async def _wait_calls(w, count, timeout=4.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while len(w.fxw.calls()) < count:
        if loop.time() > end:
            raise AssertionError(f'only {len(w.fxw.calls())} provider calls after {timeout}s')
        await asyncio.sleep(0.05)


async def _wait_dead(w, call, timeout=4.0):
    from nc_fixture.agent import alive
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while alive(call['pid']):
        if loop.time() > end:
            raise AssertionError(f"the provider process {call['pid']} did not end")
        await asyncio.sleep(0.05)


def test_tf_r5_the_conversation_lock_stays_in_force_between_tries(backing_off):
    w, run = backing_off
    w.gates_to_open.append('lk')
    w.fxw.queue({'text': PARTIAL, 'exit': 1}, {'gate': 'lk', 'text': ANSWER})

    async def go():
        first = asyncio.create_task(run.consult('advisor', MESSAGE, timeout=6))
        await _wait_calls(w, 1)
        await _wait_dead(w, w.fxw.calls()[0])
        # Between two tries: try 1 has ended and the retry is still in its
        # backoff. Releasing the lock there would let a second consult in.
        second = await run.consult('advisor', 'TF-SECOND-991: let me in?', timeout=1)
        w.gate('lk', w.fxw)
        return await asyncio.wait_for(first, 5), second
    a_reply, b_reply = asyncio.run(go())
    assert ANSWER in json.dumps(a_reply) and not a_reply.get('error'), a_reply
    assert b_reply.get('error'), 'the second consult ran between the tries'
    assert 'TF-SECOND-991' not in json.dumps(w.fxw.calls()), \
        'the second consult reached the provider between the tries'


def test_tf_r5_cancelling_the_caller_between_tries_launches_nothing_more_and_frees_the_lock(w, monkeypatch):
    w.project['scheduler'].update(retries={'backoff_seconds': [0.8, 0.8, 0.8]})
    run = build_conversation(w, monkeypatch)
    try:
        w.fxw.queue({'text': PARTIAL, 'exit': 1}, {'text': ANSWER})

        async def go():
            loop = asyncio.get_running_loop()
            first = asyncio.create_task(run.consult('advisor', MESSAGE, timeout=6))
            await _wait_calls(w, 1)
            await _wait_dead(w, w.fxw.calls()[0])
            died = loop.time()
            first.cancel()                 # during the backoff, before try 2
            await asyncio.gather(first, return_exceptions=True)
            # Poll past the backoff moment, asserting on every step, rather
            # than sleeping through it: a retry for a cancelled caller must
            # fail this the instant it launches.
            while loop.time() < died + 0.8 + 0.4:
                assert len(w.fxw.calls()) == 1, 'a retry was launched for a cancelled caller'
                await asyncio.sleep(0.02)
            settled = len(w.fxw.calls())
            w.fxw.queue({'text': ANSWER + '-2'})
            after = await run.consult('advisor', 'TF-AFTER-CANCEL-992: still usable?', timeout=4)
            return settled, after
        settled, after = asyncio.run(go())
        # Today (no retries yet) the call may already have failed before the
        # cancel lands; once TF retries exist, the cancel lands in the backoff.
        # Either way, nothing more may launch and the lock must be free again.
        assert settled == 1, 'a retry was launched for a cancelled caller'
        assert not after.get('error') and ANSWER in json.dumps(after), \
            f'the cancelled consult leaked its conversation lock: {after}'
    finally:
        teardown_conversation(w, run)


def test_tf_r5_a_spend_cap_reached_between_tries_stops_the_retries(capped):
    """A guard: the cap's own vocabulary is SC's `limited`/`spend_cap`; what
    TF adds is that a retry must not be launched past it."""
    w, run = capped
    w.fxw.queue({'text': PARTIAL, 'exit': 1, 'cost': 0.12}, {'text': ANSWER})

    async def go():
        return await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=5), 8)
    reply = asyncio.run(go())
    blob = json.dumps(reply).lower()
    assert ANSWER not in blob, reply
    assert PARTIAL not in str(reply.get('reply', ''))
    assert any(word in blob for word in ('spend', 'cap', 'limited')), \
        f'the reply must name the limitation that stopped it: {reply}'
    assert len(w.fxw.calls()) == 1, 'a retry was launched past the spend cap'


def test_tf_r5_authorisation_stays_in_force_between_tries(sole_slot):
    """TF-R5: what authorised the call stays in force between its tries. The
    provider admits one run at a time, so a retry that had to earn a fresh
    authorisation would count its own dead try as the occupant and be refused
    (or wait on itself); the reply must still arrive, and the authorisation
    must not leak either — a later consult on the same provider still runs."""
    w, run = sole_slot
    w.fxw.queue({'text': PARTIAL, 'exit': 1}, {'text': ANSWER})

    async def go():
        return await asyncio.wait_for(run.consult('advisor', MESSAGE, timeout=3), WAIT)
    reply = asyncio.run(go())
    assert ANSWER in json.dumps(reply) and not reply.get('error'), \
        f'the retry lost the call\'s authorisation: {reply}'
    assert len(w.fxw.calls()) == 2

    w.fxw.queue({'text': ANSWER + '-2'})

    async def again():
        return await asyncio.wait_for(
            run.consult('advisor', 'TF-AFTER-993: one more question?', timeout=3), WAIT)
    after = asyncio.run(again())
    assert not after.get('error') and ANSWER in json.dumps(after), \
        f'the retried consult left its authorisation or slot held: {after}'
    assert len(w.fxw.calls()) == 3
