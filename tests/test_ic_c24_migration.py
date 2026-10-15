"""IC-C24: legacy alias migration imports only its declared session payload."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_prov as ip
from nc_fixture.m4_world import commit_entry, finding, verdict_entry

@pytest.mark.parametrize('recorded,payload,outcome', [(True,True,'resumed'),(True,False,'failed'),(False,True,'fresh')])
def test_ic_c24_local_alias_migration_preserves_only_declared_payload(tmp_path, monkeypatch, recorded, payload, outcome):
    w = a.loop_world(tmp_path, monkeypatch, kind='local', payload=payload, recorded=recorded, plant_forbidden=True)
    try:
        w.fxw.queue(commit_entry('a.txt','one\n','r1'),commit_entry('b.txt','two\n','r2'))
        w.fxr.queue(verdict_entry('rejected',[finding('redo')],tag='R1',gate='upgrade'),
                    verdict_entry('approved',tag='R2'))
        top,wk,rv = ip.start_pair(w)
        w.wait_spawn('R1',w.fxr,timeout=a.WAIT)
        before_resume = w.runs(rv)[0].get('session_resume')
        w.set_executor('isolated')
        w.gate('upgrade', w.fxr)
        w.until(lambda: len(w.runs(rv)) >= 2, a.WAIT, what='migrated attempt')
        w.until(lambda: 'session_resume' in w.runs(rv)[-1], a.WAIT, what='migration outcome')
        assert w.runs(rv)[-1]['session_resume'] == outcome, w.get(rv)
        assert w.runs(rv)[0].get('session_resume') == before_resume, 'migration rewrote old resume evidence'
        if outcome != 'failed':
            w.until(lambda: len(a.loop_reports(w)) == 2, a.WAIT, what='second payload report')
            first, second = a.loop_reports(w)
            assert first['home'] != second['home']
            assert 'unrelated-profile' not in second['files']
            assert 'credential.secret' not in second['session_files']
            if outcome == 'resumed': assert second['prior']['nonce'] == 'alias-payload'
    finally:
        w.close()


def test_ic_c24_prior_unsupported_resume_record_is_not_rewritten(tmp_path, monkeypatch):
    from ic_s3 import wait_done
    w = a.World(tmp_path,monkeypatch)
    try:
        w.configure('none'); w.start()
        node = w.launch('session')
        wait_done(w,node,a.WAIT)
        assert a.resume(w,node) == 'unsupported'
        w.fake.entry['session'] = {'mode':'export','path':'sessions','resumed_event':'session_resumed','resume_deadline_seconds':0.25}
        w.p.write(); w._reset_server()
        w.reopen(node)
        w.wait_runs(node,2,a.WAIT)
        assert w.runs(node)[0]['session_resume'] == 'unsupported'
    finally:
        w.close()
