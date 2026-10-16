"""IC-C24 / D-S4A.3: legacy session_store uses its declared profile root."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_prov as ip
from nc_fixture.m4_world import commit_entry, finding, verdict_entry

pytestmark = pytest.mark.ic_milestone(4)


@pytest.mark.parametrize('location', ['profile_env', 'profile_default'])
def test_ic_c24_legacy_profile_session_payload_migrates(tmp_path, monkeypatch, location):
    w = a.loop_world(tmp_path, monkeypatch, kind='local')
    try:
        profile = tmp_path / 'legacy-private-profile'
        profile.mkdir()
        # Mirror Codex's session_store declaration and the public handover
        # resolver: profile_env overrides profile_default, dir is relative.
        declaration = w.fxr.entry
        declaration.pop('session')
        declaration['session_store'].update(profile_env='IC_LEGACY_PROFILE',
            profile_default=str(profile if location == 'profile_default' else tmp_path / 'absent-default'))
        if location == 'profile_env':
            declaration.setdefault('env', {})['IC_LEGACY_PROFILE'] = str(profile)
        script = w.fxr.dir / 'agent.py'
        source = script.read_text()
        source = source.replace("store = os.path.join(os.environ['HOME'], 'sessions')",
            "store = os.path.join(os.environ.get('IC_LEGACY_PROFILE', " + repr(str(profile)) + ") if os.environ.get('MULTIAGENTS_RPC_TOKEN') else os.environ['HOME'], 'sessions')")
        script.write_text(source)
        w.write_config()
        w.fxw.queue(commit_entry('a.txt','one\n','r1'), commit_entry('b.txt','two\n','r2'))
        w.fxr.queue(verdict_entry('rejected',[finding('redo')],tag='R1',gate='upgrade'),
                    verdict_entry('approved',tag='R2'))
        top, worker, reviewer = ip.start_pair(w)
        w.wait_spawn('R1',w.fxr,timeout=a.LOOP_WAIT)
        w.until(lambda: (profile / 'sessions' / 'payload.json').exists(), a.WAIT, what='legacy profile payload')
        w.set_executor('isolated')
        w.gate('upgrade',w.fxr)
        w.until(lambda: len(w.runs(reviewer)) >= 2 and 'session_resume' in w.runs(reviewer)[-1],
                a.LOOP_WAIT, what='migration outcome')
        assert w.runs(reviewer)[-1]['session_resume'] == 'resumed', w.get(reviewer)
        w.until(lambda: len(a.loop_reports(w)) == 2, a.LOOP_WAIT, what='migrated payload report')
        assert a.loop_reports(w)[1]['prior']['nonce'] == 'alias-payload'
    finally:
        w.close()
