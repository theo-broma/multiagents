"""FC3/F114: start failures, timeouts, and script-owned exit codes."""
from types import SimpleNamespace
import pytest
from multiagents import budget, scripts
from multiagents.providers import Provider

ACTION_TIMEOUT = 0.2


def setup_script(tmp_path, content, mode=0o755):
    folder = tmp_path / 'providers'
    folder.mkdir()
    path = folder / 'fc-script'
    path.write_text(content)
    path.chmod(mode)
    return Provider.from_dict('fc-provider', {'bin': 'sh', 'script': 'fc-script'}), path


def run(tmp_path, provider):
    return scripts.run_action('fc-provider', provider, SimpleNamespace(kind='local'),
                              'budget', tmp_path, timeout=ACTION_TIMEOUT)


@pytest.mark.parametrize('name,value', [('EXIT_TIMEOUT', 124), ('EXIT_NOT_STARTED', 126)])
def test_fc3_exposes_named_exit_constants(name, value):
    assert getattr(scripts, name, None) == value


@pytest.mark.parametrize('content,mode,reason', [
    ('#!/bin/sh\nexit 0\n', 0o644, 'not executable'),
    ('exit 0\n', 0o755, 'shebang'),
    ('#!/nonexistent/fc-interpreter\nexit 0\n', 0o755, 'No such file or directory'),
])
def test_fc3_start_failures_return_126_with_existing_reason(tmp_path, content, mode, reason):
    provider, path = setup_script(tmp_path, content, mode)
    code, out, err = run(tmp_path, provider)
    assert code == 126
    assert out == ''
    assert reason in err
    assert str(path) in err


def test_fc3_started_action_timeout_stays_124(tmp_path):
    provider, _ = setup_script(tmp_path, '#!/bin/sh\nexec sleep 10\n')
    code, out, err = run(tmp_path, provider)
    assert code == 124
    assert out == ''
    assert 'TimeoutExpired' in err


@pytest.mark.parametrize('code', [0, 124, 126, 127])
def test_fc3_script_can_itself_return_reserved_codes(tmp_path, code):
    provider, _ = setup_script(tmp_path, f'#!/bin/sh\necho own-output\necho own-reason >&2\nexit {code}\n')
    assert run(tmp_path, provider) == (code, 'own-output\n', 'own-reason\n')


def test_fc3_missing_interpreter_budget_reports_script_failure(tmp_path):
    provider, path = setup_script(tmp_path, '#!/nonexistent/fc-interpreter\n')
    reading = budget.read_provider('fc-provider', provider, SimpleNamespace(kind='local'),
                                   tmp_path, use_cache=False)
    assert reading.known is False
    assert reading.source == 'script'
    assert 'No such file or directory' in reading.note
    assert str(path) in reading.note


def test_fc3_script_exit_127_still_allows_builtin_fallback(tmp_path):
    provider, _ = setup_script(tmp_path, '#!/bin/sh\nexit 127\n')
    reading = budget.read_provider('fc-provider', provider, SimpleNamespace(kind='local'),
                                   tmp_path, use_cache=False)
    assert reading.known is False
    assert reading.source != 'script'


def test_fc3_no_script_returns_127_and_budget_uses_builtin_reader(tmp_path):
    """FC3/F114: unresolved scripts retain the built-in budget fallback."""
    provider = Provider.from_dict('agy', {'bin': 'sh', 'script': 'missing-fc-script'})
    executor = SimpleNamespace(kind='local')
    code, out, err = scripts.run_action('agy', provider, executor, 'budget',
                                        tmp_path, timeout=ACTION_TIMEOUT)
    assert code == 127
    assert out == ''
    assert err == "no script for provider 'agy'"

    reading = budget.read_provider('agy', provider, executor, tmp_path,
                                   use_cache=False)
    builtin = budget.read_agy()
    assert reading.provider == 'agy'
    assert reading.known is builtin.known
    assert reading.source == builtin.source == 'stream usage'
    assert reading.note == builtin.note
    assert reading.severity == builtin.severity
