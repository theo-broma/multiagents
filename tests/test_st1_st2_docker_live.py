"""ST1-R4 and ST2-R1/R2: opt-in behavior through a real Docker daemon.

Uses the existing SG live-world fixture, and always removes every container
and the test-created network. No real-Docker subprocess is issued unless
SG_TEST_DOCKER=1. External HTTPS probes are bounded by WAIT_SECONDS.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from multiagents.executor.docker import DockerExecutor

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import WAIT_SECONDS, owner_labels

pytestmark = pytest.mark.skipif(os.environ.get('SG_TEST_DOCKER') != '1', reason='real Docker is opt-in: SG_TEST_DOCKER=1')


def docker(*args):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=WAIT_SECONDS)


@pytest.fixture
def live(tmp_path, st_live_docker):
    # Lazy import: the existing readiness check itself touches Docker, and
    # must run only after our opt-in skip is evaluated.
    import test_sandbox_git_docker_live as sg
    reason = sg.docker_ready()
    if reason:
        pytest.skip(reason)
    world = sg.Live(tmp_path)
    try:
        up = world.p.cli('docker', 'up', timeout=WAIT_SECONDS)
        assert up.returncode == 0, up.stdout + up.stderr
        yield world
    finally:
        world.cleanup()
        # The second test may create sidecars. SG's cleanup owns workspace.
        docker('rm', '-f', f'multiagents-proxy-{world.p.paths.slug}')
        docker('rm', '-f', f'multiagents-auth-{world.p.paths.slug}')
        docker('network', 'rm', f'multiagents-net-{world.p.paths.slug}')
        for name in (world.container, f'multiagents-proxy-{world.p.paths.slug}', f'multiagents-auth-{world.p.paths.slug}'):
            assert docker('inspect', name).returncode != 0, f'test teardown left {name} behind'


def test_st1_r4_existing_live_fixture_has_no_restart_and_controller_owner(live, request):
    result = docker('inspect', live.container)
    assert result.returncode == 0, result.stderr
    box = json.loads(result.stdout)[0]
    assert box['HostConfig']['RestartPolicy']['Name'] == 'no'
    expected = {'multiagents.test.owner': request.config.st_docker_owner}
    assert expected.items() <= box['Config']['Labels'].items()


def test_st2_r1_r2_workspace_uses_updated_proxy_without_manual_action(live):
    # Derive the configured default image from the public executor property.
    ex = DockerExecutor({'image': 'multiagents/workspace:latest', 'network': 'allowlist',
                         'mount_cli_from_host': False, 'egress_allowlist': ['example.com']},
                        live.p.paths, {}, live.p.paths.config)
    proxy_image = ex.proxy_image
    if docker('image', 'inspect', proxy_image).returncode:
        pytest.skip(f'proxy image is not built: {proxy_image}')
    config_file = live.p.paths.config / 'project.yaml'
    config = yaml.safe_load(config_file.read_text())
    config['executor'] = {'kind': 'docker', 'docker': ex.config}
    config_file.write_text(yaml.safe_dump(config))
    # The SG fixture initially uses network none. Replace its test workspace
    # once to install the allowlist environment; refresh below changes only proxy.
    assert docker('rm', '-f', live.container).returncode == 0
    assert ex.ensure_running().get('ok')
    before = json.loads(docker('inspect', live.container).stdout)[0]['Id']
    def probe(host):
        return docker('exec', live.container, 'curl', '--max-time', str(WAIT_SECONDS - 1),
                      '-sS', '-o', '/dev/null', '-w', '%{http_code}', f'https://{host}/')
    control = probe('example.com')
    assert control.returncode == 0 and control.stdout not in ('000', '403'), control.stderr
    denied = probe('example.org')
    assert denied.returncode != 0 or denied.stdout in ('000', '403')
    ex.config['egress_allowlist'] = ['example.com', 'example.org']
    assert ex.ensure_running().get('ok')
    added = probe('example.org')
    assert added.returncode == 0 and added.stdout not in ('000', '403'), added.stderr
    assert json.loads(docker('inspect', live.container).stdout)[0]['Id'] == before
