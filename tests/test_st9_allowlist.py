"""ST9: F10 regression contract through generated filters and CLI diagnostics.

The PSL cases are real ICANN/private rules: co.uk, github.io, *.ck, !www.ck.
No synthetic suffix rules or private validator are used.
"""
import sys
import os
import socket
import urllib.request
from pathlib import Path

import pytest
import yaml

from multiagents.executor.docker import DockerExecutor
from multiagents.config import load as load_config

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import DockerWorld, WAIT_SECONDS, admits
import sv_harness


def write_filter(tmp_path, entries):
    ex = DockerExecutor({'egress_allowlist': entries})
    directory = ex.write_proxy_config(tmp_path / 'proxy')
    return ex, (directory / 'filter').read_text(), (directory / 'tinyproxy.conf').read_text()


@pytest.mark.parametrize(('entry', 'attack'), [
    ('com', 'unapproved.com'),
    ('co.uk', 'unapproved.co.uk'),
    ('github.io', 'unapproved.github.io'),
    ('tenant.ck', 'unapproved.tenant.ck'),  # *.ck makes tenant.ck a public suffix
    ('1.1', '192.168.1.1'),
    ('168.1.1', '192.168.1.1'),
    ('192.168.1', '10.192.168.1'),
])
def test_st9_r1_refused_suffix_is_absent_from_filter_and_cannot_admit_attack(tmp_path, entry, attack):
    _, text, _ = write_filter(tmp_path, [entry, 'example.com'])
    assert not admits(text, attack), text
    assert not admits(text, entry), text
    assert admits(text, 'example.com'), 'valid entries remain active'


@pytest.mark.parametrize('entry', ['COM.', 'Co.Uk.', 'GITHUB.IO.', 'Tenant.CK.'])
def test_st9_r1_normalisation_precedes_suffix_checks(tmp_path, entry):
    _, text, _ = write_filter(tmp_path, [entry])
    assert not admits(text, entry.rstrip('.').lower()), text
    assert not admits(text, 'anything.' + entry.rstrip('.').lower()), text
    # A writer that simply preserves the forbidden entry (including its dot)
    # must not pass because only the normalized destination was probed.
    assert not admits(text, entry), text
    assert not admits(text, 'anything.' + entry), text


@pytest.mark.parametrize('entry', ['www.ck', 'WWW.CK.', 'child.www.ck'])
def test_st9_r1_psl_exception_name_is_accepted(tmp_path, entry):
    _, text, _ = write_filter(tmp_path, [entry])
    normalized = entry.lower().rstrip('.')
    assert admits(text, normalized), text
    assert admits(text, 'api.' + normalized), text
    assert not admits(text, 'evil-' + normalized), text


@pytest.mark.parametrize('entry', ['example.co.uk', 'tenant.github.io', 'child.tenant.ck', '1.example.com'])
def test_st9_r1_registrable_domains_beneath_psl_rules_are_accepted(tmp_path, entry):
    _, text, _ = write_filter(tmp_path, [entry])
    assert admits(text, entry), text
    assert admits(text, 'api.' + entry), text
    assert not admits(text, 'evil-' + entry), text


@pytest.mark.parametrize('entry', ['BÜCHER.de.', 'xn--bcher-kva.DE.', 'bücher.de'])
def test_st9_r1_idna_alabel_lowercase_and_trailing_dot_normalisation(tmp_path, entry):
    _, text, _ = write_filter(tmp_path, [entry])
    assert admits(text, 'xn--bcher-kva.de'), text
    assert admits(text, 'api.xn--bcher-kva.de'), text
    assert not admits(text, 'evil-xn--bcher-kva.de'), text


@pytest.mark.parametrize('entry', ['::1', '[::1]', '2001:db8::1', '[2001:db8::1]'])
def test_st9_r1_ipv6_remains_refused(tmp_path, entry):
    _, text, _ = write_filter(tmp_path, [entry, 'example.com'])
    assert not admits(text, entry), text
    assert admits(text, 'example.com')


@pytest.mark.parametrize('address', ['0.0.0.0', '192.168.1.1', '255.255.255.255'])
def test_st9_r1_whole_ipv4_matches_only_exact_address(tmp_path, address):
    _, text, _ = write_filter(tmp_path, [address])
    assert admits(text, address)
    for host in ['api.' + address, '10.' + address, address + '.example.com', 'evil-' + address]:
        assert not admits(text, host), (host, text)


def test_st9_r1_pinned_psl_checks_work_with_network_unavailable(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        raise AssertionError('allowlist validation must not download a PSL at runtime')
    monkeypatch.setattr(socket, 'create_connection', offline)
    monkeypatch.setattr(urllib.request, 'urlopen', offline)
    _, text, _ = write_filter(tmp_path, ['co.uk', 'github.io', 'tenant.ck', 'www.ck', 'example.com'])
    assert not admits(text, 'attack.co.uk')
    assert not admits(text, 'attack.github.io')
    assert not admits(text, 'attack.tenant.ck')
    assert admits(text, 'www.ck')
    assert admits(text, 'example.com')


def test_st9_r2_hostname_matching_keeps_domain_boundaries(tmp_path):
    _, text, _ = write_filter(tmp_path, ['example.com'])
    for host in ['example.com', 'api.example.com', 'nested.api.example.com']:
        assert admits(text, host)
    for host in ['evil-example.com', 'example.com.evil.net', 'notexample.com']:
        assert not admits(text, host)


@pytest.mark.parametrize('entries', [[], ['com'], ['com', 'co.uk', 'github.io', 'tenant.ck', '1.1', '::1']])
def test_st9_r2_no_valid_entries_generates_deny_all(tmp_path, entries):
    _, text, conf = write_filter(tmp_path, entries)
    for host in ['example.com', 'unapproved.co.uk', 'www.ck', '192.168.1.1', 'localhost', '']:
        assert not admits(text, host), (host, text)
    directives = [line.lower().split() for line in conf.splitlines() if line.strip() and not line.startswith('#')]
    assert ['filterdefaultdeny', 'yes'] in directives


@pytest.mark.parametrize(('entry', 'reason_words'), [
    ('com', ('single', 'label', 'suffix', 'registrable', 'dot')),
    ('co.uk', ('public', 'suffix', 'registrable')),
    ('github.io', ('public', 'suffix', 'registrable')),
    ('tenant.ck', ('public', 'suffix', 'registrable')),
    ('1.1', ('fragment', 'ip', 'numeric')),
    ('::1', ('ipv6',)),
])
def test_st9_r2_filter_write_reports_entry_and_reason(tmp_path, capsys, caplog, entry, reason_words):
    write_filter(tmp_path, [entry])
    captured = capsys.readouterr()
    report = captured.out + captured.err + caplog.text
    assert entry in report, report
    assert any(word in report.lower() for word in reason_words), report


def test_st9_r2_refused_entry_is_nonfatal_and_config_is_not_rewritten(tmp_path, monkeypatch):
    world = DockerWorld(tmp_path, monkeypatch)
    ex = world.executor(network='allowlist', egress_allowlist=['com'])
    config_file = world.paths.config / 'project.yaml'
    original = '# user-maintained\nexecutor:\n  kind: docker\n  docker:\n    egress_allowlist: [com]\n'
    config_file.write_text(original)
    assert ex.configuration_problems() == []
    outcome = ex.ensure_running()
    assert outcome.get('ok'), outcome
    applied = world.box(ex.proxy_container)['loaded_filter']
    assert not admits(applied, 'unapproved.com')
    assert config_file.read_text() == original
    assert ex.config['egress_allowlist'] == ['com']


@pytest.mark.parametrize('command', [('run', '--no-launch'), ('doctor',), ('docker', 'up')])
def test_st9_r2_cli_reports_refused_entry_without_rewriting_project(tmp_path, monkeypatch, command):
    # sv's provider is a local executable; every other provider is disabled.
    project = sv_harness.Project(tmp_path, executor='docker')
    world = DockerWorld(tmp_path / 'daemon-world', monkeypatch)
    # Avoid ambient installed model CLIs even in doctor, which checks disabled
    # credential owners. Configure every shipped provider explicitly and keep
    # PATH to the daemon double plus system utilities.
    providers_path = project.paths.config / 'providers.yaml'
    providers_data = yaml.safe_load(providers_path.read_text())
    for name in load_config(project.paths).providers:
        if name != 'svstub':
            providers_data['providers'][name] = {'enabled': False, 'bin': 'st9-uninstalled-provider', 'bin_search': []}
    providers_data['providers'].pop('opencode', None)
    providers_path.write_text(yaml.safe_dump(providers_data))
    monkeypatch.setenv('PATH', os.environ['ST_TEST_DOCKER_GUARD'] + os.pathsep + str(world.daemon) + ':/usr/bin:/bin')
    path = project.paths.config / 'project.yaml'
    data = yaml.safe_load(path.read_text())
    data['executor'] = {'kind': 'docker', 'docker': {'image': 'img', 'network': 'allowlist', 'mount_cli_from_host': False, 'egress_allowlist': ['com']}}
    path.write_text(yaml.safe_dump(data))
    original = path.read_bytes()
    result = project.cli(*command, timeout=WAIT_SECONDS)
    output = result.stdout + result.stderr
    assert 'com' in output, output
    assert any(word in output.lower() for word in ('suffix', 'single label', 'single-label', 'registrable')), output
    assert path.read_bytes() == original
    if command[0] in ('run', 'docker'):
        assert result.returncode == 0, output
