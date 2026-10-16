"""Adversary, slice 4A: the per-attempt RPC endpoint as a trust boundary.

Each test here failed on the branch it was written against (nd-905bbdf0); it
states the contract clause it holds the implementation to.
"""
import json
import sys
from pathlib import Path

import pytest
from multiagents.paths import ProjectPaths

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s3 as s3  # noqa: E402
import ic_s4a as a  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    s = a.World(tmp_path, monkeypatch, backend='reference')
    try:
        yield s
    finally:
        s.close()


def held_endpoint(world):
    world.configure(rpc=True); world.start()
    node = world.launch('scoped_rpc', requests=[a.request()], hold=True)
    report = world.report(node)
    assert report['replies'][0].get('ok') is True, report
    return node, report['endpoint']


def refuses(path):
    with pytest.raises(OSError):
        with a.connect(str(path)):
            pass


# --- forged principals -------------------------------------------------------

@pytest.mark.parametrize('field,value', [('token', 'forged-token'), ('subject', 'root'),
                                         ('root', True)])
def test_identity_field_inside_args_is_forbidden(world, field, value):
    """D-S4A.1 Envelope / IC-C20: "a request carrying a token or a
    caller-identity field" is refused with `forbidden`. The envelope check only
    looks at top-level keys; the same field one level down, in `args`, of an
    operation that ignores unknown arguments, is answered `ok`."""
    world.configure(rpc=True); world.start()
    node = world.launch('scoped_rpc', requests=[a.request('list_nodes', {field: value})])
    s3.wait_done(world, node, a.WAIT)
    reply = world.report(node)['replies'][0]
    assert reply.get('error', {}).get('error') == 'forbidden', reply


# --- scheduler restart -------------------------------------------------------

def test_restart_with_revoked_authority_holds_the_running_attempt(world):
    """D-S4A.2 Scheduler restart: "An attempt whose authority cannot be
    confirmed gets no listener and is held (K6)." The run's capability is
    revoked while its container keeps running; at restart the listener is
    (correctly) not recreated, but the node is left `running`, unheld, with a
    live workload and no RPC."""
    node, path = held_endpoint(world)
    run_id = world.runs(node)[-1]['run_id']
    world.revoke(run_id)
    world.restart(hard=True)
    refuses(path)
    world.wait(lambda: world.get(node)['state'] == 'held', a.WAIT,
               'held: authority cannot be confirmed')


# --- orphan endpoints --------------------------------------------------------

@pytest.mark.parametrize('name', ['stray', 'att_' + 'A' * 32])
def test_every_unrecorded_endpoint_directory_is_reported(world, name):
    """IC-C21 / K18.2: "A directory there with no recorded attempt is an
    orphan endpoint" and is reported with reason `orphan_endpoint`. A directory
    whose name is not shaped like an attempt id is still unrecorded, but is
    silently left out of `orphans` (and so can never be removed through
    `remove-orphan`)."""
    world.configure(rpc=True)
    directory = ProjectPaths(world.root).scheduler / 'endpoints' / name
    directory.mkdir(parents=True)
    world.start()
    result = world.cli('scheduler', 'orphans', timeout=a.WAIT)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)['orphans']
    assert any(r.get('endpoint_id') == name and r.get('reason') == 'orphan_endpoint'
               for r in rows), rows
