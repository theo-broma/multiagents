"""ST9-R1/F10 and ST1-R3/ST2-R1: stabilization Decision 14 regressions."""
import sys
from pathlib import Path

import pytest
import yaml

from multiagents import publicsuffix
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import DockerWorld, admits, mutations


@pytest.fixture
def world(tmp_path, monkeypatch):
    return DockerWorld(tmp_path, monkeypatch)


@pytest.mark.parametrize('root', ['run.app', 'digitaloceanspaces.com', 'linodeobjects.com', 'kobe.jp', 'ck'])
def test_st9_r1_d14_wildcard_roots_cannot_admit_every_tenant(world, root):
    ex = world.executor(network='allowlist', egress_allowlist=[root, 'example.com'])
    allowed, diagnostics = ex.allowlist_entries()
    assert root not in allowed
    assert any(root in diagnostic for diagnostic in diagnostics)
    directory = ex.write_proxy_config(world.base / 'filter-config')
    text = (directory / 'filter').read_text()
    assert not admits(text, root)
    assert not admits(text, 'another-tenant.' + root)
    assert admits(text, 'api.example.com')


def test_st9_r1_d14_exception_at_wildcard_root_wins(monkeypatch):
    monkeypatch.setattr(publicsuffix, '_rules', lambda: (set(), {'root.example'}, {'root.example'}))
    assert not publicsuffix.is_public_suffix('root.example')


@pytest.mark.parametrize('exception', ['www.ck', 'city.kobe.jp'])
def test_st9_r1_d14_pinned_exceptions_still_admit_subdomains(world, exception):
    ex = world.executor(egress_allowlist=[exception])
    assert ex.allowlist_entries() == ([exception], [])
    text = (ex.write_proxy_config(world.base / 'filter-config') / 'filter').read_text()
    assert admits(text, 'api.' + exception)


def test_st9_r1_d14_shipped_allowlist_has_no_wildcard_roots():
    defaults = Path(__file__).parents[1] / 'src' / 'multiagents' / 'defaults' / 'project.yaml'
    entries = yaml.safe_load(defaults.read_text())['executor']['docker']['egress_allowlist']
    entries += [name + '.googleapis.com' for name in ['cloudcode-pa', 'daily-cloudcode-pa', 'oauth2', 'www', 'play']]
    _, wildcard, _ = publicsuffix._rules()
    assert not set(entries) & wildcard
    # googleapis.com is already refused by ST9; narrowing it is a separate change.
    entries = [entry for entry in entries if entry != 'googleapis.com']
    ex = DockerExecutor({'egress_allowlist': entries}, ProjectPaths(defaults.parent), {}, defaults.parent)
    assert ex.allowlist_entries() == (entries, [])


def started(world):
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    assert ex.ensure_proxy().get('ok')
    world.clear_calls()
    return ex


@pytest.mark.parametrize('status', ['running', 'exited'])
@pytest.mark.parametrize('reconcile', ['different-digest', 'absent-receipt'])
def test_st1_r3_st2_r1_d14_replacement_ignores_missing_old_sources(world, status, reconcile):
    ex = started(world)
    missing = world.base / 'vanished-old-source'
    state = world.read()
    box = state['containers'][ex.proxy_container]
    box['State']['Status'] = status
    box['Mounts'].append({'Type': 'bind', 'Source': str(missing), 'Destination': '/obsolete', 'RW': False})
    box['NetworkSettings']['Networks']['additional-network'] = {}
    world.write(state)
    if reconcile == 'different-digest':
        ex.config['egress_allowlist'] = ['new.example.net']
    else:
        from multiagents.paths import state_root
        (state_root() / 'container-state' / ex.slug / 'egress-proxy' / 'applied.json').unlink()
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    commands = mutations(world.calls(), ex.proxy_container)
    assert any(argv[0] == 'rm' for argv in commands)
    assert any(argv[0] == 'run' for argv in commands)
    assert not any(argv[0] == 'start' for argv in commands)
    replacement = world.box(ex.proxy_container)
    assert 'additional-network' in replacement['NetworkSettings']['Networks']
    assert admits(replacement['loaded_filter'], ex.config['egress_allowlist'][0])
    assert not missing.exists()


def test_st1_r3_d14_start_existing_proxy_still_checks_missing_sources(world):
    ex = started(world)
    missing = world.base / 'vanished-current-source'
    state = world.read()
    box = state['containers'][ex.proxy_container]
    box['State']['Status'] = 'exited'
    box['Mounts'].append({'Type': 'bind', 'Source': str(missing), 'Destination': '/current', 'RW': False})
    world.write(state)
    outcome = ex.ensure_proxy()
    assert not outcome.get('ok'), outcome
    assert str(missing) in str(outcome)
    assert 'recreat' in str(outcome).lower()
    assert not mutations(world.calls(), ex.proxy_container)
    assert not missing.exists()


def test_st1_r3_st2_r1_d14_missing_replacement_source_still_refuses(world, monkeypatch):
    ex = started(world)
    ex.config['egress_allowlist'] = ['new.example.net']
    write_config = ex.write_proxy_config

    def write_with_missing_filter(target):
        directory = write_config(target)
        (directory / 'filter').unlink()
        return directory

    monkeypatch.setattr(ex, 'write_proxy_config', write_with_missing_filter)
    outcome = ex.ensure_proxy()
    assert not outcome.get('ok'), outcome
    assert str(world.paths.config / 'proxy' / ex.slug / 'filter') in str(outcome)
    assert not any(argv[0] in ('run', 'start') for argv in mutations(world.calls(), ex.proxy_container))


@pytest.mark.parametrize('status', ['running', 'exited'])
def test_st2_r3_unchanged_digest_does_not_rewrite_proxy_files(world, monkeypatch, status):
    ex = started(world)
    state = world.read()
    state['containers'][ex.proxy_container]['State']['Status'] = status
    world.write(state)
    directory = world.paths.config / 'proxy' / ex.slug
    files = [directory / 'filter', directory / 'tinyproxy.conf']
    before = [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files]
    write_text = Path.write_text

    def record_write(path, *args, **kwargs):
        assert path not in files, 'an unchanged filter must not be rewritten'
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', record_write)
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files] == before
