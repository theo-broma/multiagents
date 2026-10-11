"""DJ-R9: every subcommand refuses the docker agent signal before acting."""
import pytest
from test_dj_support import jobs


@pytest.mark.parametrize('args', [
    ['run','--owner','orchestrator','--','sh','-c','exit 0'],
    ['list','--json'], ['wait','unknown','--timeout','0'],
    ['stop','unknown'], ['ack','unknown'], ['watch','--owner','orchestrator'], ['prune'],
])
def test_dj_r9_every_subcommand_refuses_agent_container(jobs, args):
    env = dict(jobs.env, MULTIAGENTS_CONTAINER='multiagents-test-agent')
    result = jobs.cli(*args, env=env, input='{"session_id":"unused"}')
    assert result.returncode == 1, result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1 and 'container' in lines[0].lower()
    assert not result.stdout
