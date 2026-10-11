"""DJ-R8 shipped root instructions describe launch, notification and fallback."""
import re
import pytest
from test_dj_support import ROOT


@pytest.mark.parametrize('relative', [
    'src/multiagents/defaults/agents/team/_orchestrator.md',
    'src/multiagents/defaults/agents/team/_initializer.md', 'AGENTS.md',
])
def test_dj_r8_root_instructions_explain_detached_jobs(relative):
    text = (ROOT / relative).read_text().lower()
    assert 'bg run' in text
    assert 'run_in_background' in text
    assert 'hook' in text and ('notification' in text or 'notify' in text)
    assert 'bg list' in text and 'bg ack' in text
    assert 'interrupt' in text and 'api' in text and 'error' in text
    assert re.search(r'unacknowledged|unacked',text)
