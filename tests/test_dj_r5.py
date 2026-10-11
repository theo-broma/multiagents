"""DJ-R5: exercise public root CLI entry points against an argv-capturing native.

Only authentication, quota and remote model-catalog reads are substituted.
The driver and shipped Claude launch script remain real. No Claude is run.
"""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys

import pytest
import yaml
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.providers import load_providers
from test_dj_support import ROOT, CLI_BOUND, eventually


@pytest.fixture
def root_launch(tmp_path):
    paths = ProjectPaths(tmp_path / 'project'); paths.ensure()
    (paths.root / 'BRIEF.md').write_text('# Test project\n')
    subprocess.run(['git','init','-q',str(paths.root)],check=True)
    native = tmp_path / 'fake-claude'
    captured = tmp_path / 'argv.json'
    native.write_text('#!' + sys.executable + '\n' +
        'import json,os,pathlib,sys,time\n' +
        f'pathlib.Path({str(captured)!r}).write_text(json.dumps({{"argv":sys.argv[1:],"pid":os.getpid(),"session":os.environ.get("MULTIAGENTS_SESSION_ID"),"children":[int(p) for p in pathlib.Path(f"/proc/{{os.getpid()}}/task/{{os.getpid()}}/children").read_text().split()]}}))\n' +
        'release=os.environ.get("DJ_RELEASE")\n' +
        'deadline=time.monotonic()+8\n' +
        'while release and not pathlib.Path(release).exists() and time.monotonic()<deadline: time.sleep(0.025)\n')
    native.chmod(0o755)
    (paths.config / 'project.yaml').write_text(yaml.safe_dump({
        'executor':{'kind':'local'}, 'team':'', 'bg':{'watch_timeout':180}}))
    (paths.config / 'providers.yaml').write_text(yaml.safe_dump({'providers':{
        'claude':{'bin':str(native),'budget':{'script':''}}}}))
    (paths.config / 'agents.yaml').write_text(yaml.safe_dump({'agents':{
        role:{'provider':'claude','model':'sonnet','role':role,'launch':True,
              'instructions':[]} for role in ('orchestrator','initializer')}}))
    user = Path(os.environ['HOME']) / '.claude/settings.json'
    project = paths.root / '.claude/settings.json'
    for p in (user, project):
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(b'{"hooks":{"Stop":[]},"unrelated":"preserve"}\n')
    shim = '''import os,sys
if os.environ.get("DJ_TEST_TTY"): sys.stdin.isatty = lambda: True
from unittest.mock import patch
from multiagents import cli,auth
with patch('multiagents.auth.check',return_value=auth.AuthState('claude','authenticated')), patch('multiagents.budget.read_all',return_value={}), patch('multiagents.catalog.check',return_value={'ok':False,'error':'offline test'}):
    raise SystemExit(cli.main(sys.argv[1:]))
'''
    children = []
    native_captures = []
    releases = []
    def launch(role, fresh=False, supervise=True, hold=False, tty=False):
        args = ['run' if role == 'orchestrator' else 'init-agent']
        if tty and role == 'initializer': args += ['--team','implement']
        if fresh: args += ['--fresh']
        if not supervise: args += ['--no-supervise']
        argv = [sys.executable,'-c',shim,'--path',str(paths.root),*args]
        env = dict(os.environ,PYTHONPATH=str(ROOT/'src'))
        if tty: env['DJ_TEST_TTY'] = '1'
        provider_path = paths.config / 'providers.yaml'
        provider_config = yaml.safe_load(provider_path.read_text())
        provider_env = provider_config['providers']['claude'].setdefault('env', {})
        if hold:
            release = tmp_path / f'release-{len(releases)}'; releases.append(release)
            # Provider children receive only explicitly configured extra env.
            provider_env['DJ_RELEASE'] = str(release)
        else:
            provider_env.pop('DJ_RELEASE', None)
        provider_path.write_text(yaml.safe_dump(provider_config))
        if hold:
            if captured.exists(): captured.unlink()
            child = subprocess.Popen(argv,cwd=paths.root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            children.append(child)
            def read_capture():
                try: return json.loads(captured.read_text())
                except (FileNotFoundError, json.JSONDecodeError): return None
            capture = eventually(read_capture); native_captures.append(capture)
            return capture, child, release
        result = subprocess.run(argv,cwd=paths.root,env=env,
            capture_output=True,text=True,timeout=CLI_BOUND)
        assert result.returncode == 0, result.stdout + result.stderr
        assert captured.exists(), 'fake native CLI was not launched'
        capture = json.loads(captured.read_text()); native_captures.append(capture)
        return capture
    try:
        yield paths, launch, (user,project)
    finally:
        for release in releases: release.touch()
        for child in children:
            try:
                child.communicate(timeout=CLI_BOUND)
            except subprocess.TimeoutExpired:
                child.kill(); child.communicate(timeout=CLI_BOUND)
        for capture in native_captures:
            for pid in [capture['pid'], *capture['children']]:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass


@pytest.mark.parametrize('role', ['orchestrator','initializer'])
def test_dj_r5_root_argv_has_absolute_host_settings_and_stop_hook(root_launch, role):
    paths, launch, originals = root_launch
    before = [p.read_bytes() for p in originals]
    capture = launch(role)
    argv = capture['argv']
    assert '--settings' in argv, f'{role} launched without detached-job hook settings: {argv}'
    settings = Path(argv[argv.index('--settings')+1])
    assert settings.is_absolute() and settings.is_file()
    assert settings.is_relative_to(Path(os.environ['MULTIAGENTS_STATE_DIR']))
    assert not settings.is_relative_to(paths.root)
    hooks = json.loads(settings.read_text())['hooks']['Stop']
    declared = [h for entry in hooks for h in entry.get('hooks',[])]
    assert len(declared) == 1
    hook = declared[0]
    assert hook['asyncRewake'] is True and hook['timeout'] == 180
    command = shlex.split(hook['command'])
    assert command[0] == sys.executable
    assert '-m' in command and 'bg' in command and 'watch' in command
    assert role in command and str(paths.root) in command
    assert [p.read_bytes() for p in originals] == before
    # CLI is gone: the installed command must resolve independently and exit silently.
    result = subprocess.run(command,input=json.dumps({'session_id':capture['session']}),
        env=dict(os.environ,PATH='',PYTHONPATH=str(ROOT/'src')),cwd=settings.parent,
        capture_output=True,text=True,timeout=CLI_BOUND)
    assert result.returncode == 0, result.stderr


def test_dj_r5_no_supervise_root_carries_settings(root_launch):
    role = 'orchestrator'
    _, launch, _ = root_launch
    capture = launch(role,supervise=False)
    assert '--settings' in capture['argv']


def test_dj_r5_new_session_launch_still_carries_settings(root_launch):
    _, launch, _ = root_launch
    first = launch('orchestrator')
    second = launch('orchestrator',fresh=True)
    assert first['session'] != second['session']
    assert '--settings' in first['argv'] and '--settings' in second['argv']


def test_dj_r5_spawned_agent_argv_has_no_root_hook_settings(tmp_path):
    config = load(ProjectPaths(tmp_path))
    provider = load_providers(config.providers)['claude']
    argv = provider.build_command(prompt='test',prompt_file=str(tmp_path/'prompt'),
                                  model='sonnet',workdir=str(tmp_path))
    assert '--settings' not in argv


@pytest.mark.parametrize('timeout', [0,60,119])
def test_dj_r5_config_rejects_timeout_below_120(tmp_path, timeout):
    paths = ProjectPaths(tmp_path); paths.ensure()
    (paths.config/'project.yaml').write_text(yaml.safe_dump({'bg':{'watch_timeout':timeout}}))
    with pytest.raises(ValueError,match=r'(?i)watch_timeout|bg'):
        load(paths)


@pytest.mark.parametrize('timeout', [120,3600])
def test_dj_r5_config_accepts_timeout_at_boundary(tmp_path, timeout):
    paths = ProjectPaths(tmp_path); paths.ensure()
    (paths.config/'project.yaml').write_text(yaml.safe_dump({'bg':{'watch_timeout':timeout}}))
    assert load(paths).project['bg']['watch_timeout'] == timeout


def test_dj_r5_default_hook_timeout_is_3600(root_launch):
    paths, launch, _ = root_launch
    config_file = paths.config/'project.yaml'
    project = yaml.safe_load(config_file.read_text()); project.pop('bg')
    config_file.write_text(yaml.safe_dump(project))
    capture = launch('orchestrator')
    argv = capture['argv']
    assert '--settings' in argv
    settings = json.loads(Path(argv[argv.index('--settings')+1]).read_text())
    hooks = [h for entry in settings['hooks']['Stop'] for h in entry.get('hooks',[])]
    assert len(hooks) == 1 and hooks[0]['timeout'] == 3600


@pytest.mark.parametrize('role,supervise', [('orchestrator',True),('orchestrator',False),('initializer',True)])
def test_dj_r5_binding_names_real_cli_and_refreshes_on_new_launch(root_launch, role, supervise):
    from multiagents.procs import start_time
    paths, launch, _ = root_launch
    first, process, release = launch(role,supervise=supervise,hold=True)
    directory = Path(os.environ['MULTIAGENTS_STATE_DIR'])/'bg'/paths.slug/'bindings'
    binding = directory/f"{first['session']}.json"
    assert binding.is_file(), 'driver did not bind the actual root CLI'
    value = json.loads(binding.read_text())
    assert value['version'] == 1 and value['role'] == role
    assert value['session_id'] == first['session']
    assert value['pid'] == first['pid'] and value['pid_start'] == start_time(first['pid'])
    release.touch(); process.communicate(timeout=CLI_BOUND)
    second, _, _ = launch(role,supervise=supervise,hold=True)
    refreshed = json.loads((directory/f"{second['session']}.json").read_text())
    assert refreshed['pid'] == second['pid'] != first['pid']
    assert refreshed['pid_start'] == start_time(second['pid'])


@pytest.mark.parametrize('role', ['orchestrator','initializer'])
def test_dj_r5_supervised_restart_refreshes_binding_for_replacement_cli(root_launch, role):
    paths, launch, _ = root_launch
    config_path = paths.config/'project.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['limits'] = {'restart_on_crash':True, 'restart_attempts':1,
                        'restart_delay_seconds':0, 'restart_min_runtime_seconds':0.01}
    config_path.write_text(yaml.safe_dump(config))
    provider_config = yaml.safe_load((paths.config/'providers.yaml').read_text())
    native = Path(provider_config['providers']['claude']['bin'])
    history = native.parent/'restart-history.json'
    source = native.read_text()
    injected = f'''history=pathlib.Path({str(history)!r})
seen=json.loads(history.read_text()) if history.exists() else []
seen.append({{"pid":os.getpid(),"session":os.environ.get("MULTIAGENTS_SESSION_ID")}})
history.write_text(json.dumps(seen))
if len(seen)==1: sys.exit(7)
'''
    native.write_text(source.replace('release=os.environ.get("DJ_RELEASE")',
                                    injected+'release=os.environ.get("DJ_RELEASE")'))
    capture = launch(role,tty=True)
    seen = json.loads(history.read_text())
    assert len(seen) == 2 and seen[0]['pid'] != seen[1]['pid']
    directory = Path(os.environ['MULTIAGENTS_STATE_DIR'])/'bg'/paths.slug/'bindings'
    binding = directory/f"{capture['session']}.json"
    assert binding.exists(), 'replacement CLI has no binding'
    value = json.loads(binding.read_text())
    assert value['pid'] == capture['pid'] == seen[1]['pid']
    assert value['session_id'] == capture['session'] and value['role'] == role
    assert value['pid_start']
    assert '--settings' in capture['argv']
