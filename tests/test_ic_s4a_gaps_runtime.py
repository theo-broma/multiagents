"""Slice 4A production-path regressions: IC-C20, D-S4A.4/5."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_s3 as s3

pytestmark = pytest.mark.ic_milestone(4)
CLIENT_WAIT = 3  # one local MCP client import and round trip


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = a.World(tmp_path, monkeypatch)
    try:
        yield w
    finally:
        w.close()


def test_ic_c20_real_mcp_client_uses_scoped_socket_without_token(world):
    world.configure(rpc=True)
    script = Path(world.fake.entry['bin'])
    # The provider invokes the same public MCP functions agents invoke. No
    # substitute RPC client, capability, socket rewrite, or server patch.
    client = "from multiagents import server; import json; print(json.dumps({'visible':server.list_nodes(), 'outside':server.get_node('OUTSIDE')}))"
    src = str(Path(__file__).resolve().parents[1] / 'src')
    branch = '''elif probe == "production_client":
    env = dict(os.environ, PYTHONPATH=ins['src'], MULTIAGENTS_PROJECT=ins['project'])
    env.update(ins['config_env'])
    result = subprocess.run([ins['python'], '-c', ins['client']], env=env, capture_output=True, text=True, timeout=ins['client_wait'])
    say(dict(common(), rc=result.returncode, out=result.stdout, err=result.stderr,
             token_present='MULTIAGENTS_RPC_TOKEN' in os.environ, socket=os.environ.get('MULTIAGENTS_RPC_SOCKET')))
elif probe == "plant":
'''
    script.write_text(script.read_text().replace('elif probe == "plant":\n', branch, 1))
    world.start()
    outside = world.launch('session')
    s3.wait_done(world, outside, a.WAIT)
    node = world.launch('production_client', src=src, python=sys.executable,
                        client=client.replace('OUTSIDE', outside), project=str(world.root), client_wait=CLIENT_WAIT,
                        config_env={k:v for k,v in os.environ.items() if k in ('MULTIAGENTS_CONFIG_DIR', 'MULTIAGENTS_STATE_DIR')})
    report = world.report(node)
    assert not report['token_present'] and report['socket'], report
    assert report['rc'] == 0, report
    reply = json.loads(report['out'])
    assert 'plan_revision' in reply['visible'], reply
    assert node in json.dumps(reply['visible']), reply
    assert outside not in json.dumps(reply['visible']), reply
    assert 'forbidden' in json.dumps(reply['outside']), reply


@pytest.mark.parametrize('mode', ['home', 'export'])
def test_d_s4a_5_confirmed_resume_keeps_nonzero_exit_class(world, mode):
    world.configure(mode)
    script = Path(world.fake.entry['bin'])
    script.write_text(script.read_text().replace(
        '    if ins.get("hold") or (resumed and ins.get("resume_hold")):',
        '    if resumed: sys.exit(23)\n    if ins.get("hold") or (resumed and ins.get("resume_hold")):'))
    world.start()
    node = world.launch('session')
    world.report(node)
    s3.wait_done(world, node, a.WAIT)
    world.reopen(node)
    world.report(node, 2)
    world.wait(lambda: len(world.runs(node)) >= 2 and world.runs(node)[1].get('cause'),
               a.WAIT, 'nonzero exit evidence')
    run = world.runs(node)[1]
    assert (run['session_resume'], run['cause']['class']) == ('resumed', 'crash'), run
