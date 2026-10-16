"""D-S4A.3/4: an interrupted export must not become a partial resume."""
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ic_milestone(4)


def test_d_s4a_3_interrupted_export_never_resumes_partial_payload(tmp_path, monkeypatch):
    from multiagents.paths import ProjectPaths
    from multiagents.scheduler import conversations

    monkeypatch.setenv('MULTIAGENTS_STATE_DIR', str(tmp_path / 'state'))
    root = tmp_path / 'project'
    root.mkdir()
    paths = ProjectPaths(root)
    provider = SimpleNamespace(name='probe', container_account='', family='', session_store=None,
        session={'mode':'export', 'path':'sessions', 'resumed_event':'session_resumed', 'resume_deadline_seconds':1})
    attempt = {'node_id':'nd-export', 'attempt_id':'first', 'binding':{'model':'probe/m1'}}
    home = tmp_path / 'first-home'
    payload = home / 'sessions'
    payload.mkdir(parents=True)
    conversations.prepare(paths, attempt, provider, home=home)
    for name in ('a.json', 'b.json'):
        (payload / name).write_text('previous-' + name)
    conversations.finish(paths, attempt, provider, 'ses_previous', home=home)
    for name in ('a.json', 'b.json'):
        (payload / name).write_text('replacement-' + name)

    replace = os.replace
    def disk_failure(source, destination, *args, **kwargs):
        # Filesystem fault at publication of the second payload file. This
        # patches an OS operation, not the conversation implementation.
        if Path(destination).name == 'b.json':
            raise OSError(errno.EIO, 'injected copy interruption')
        return replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, 'replace', disk_failure)
        try:
            conversations.finish(paths, attempt, provider, 'ses_replacement', home=home)
        except (OSError, conversations.ConversationError):
            pass

    next_attempt = {'node_id':'nd-export', 'attempt_id':'next', 'binding':{'model':'probe/m1'}}
    restored = tmp_path / 'next-home'
    try:
        plan = conversations.prepare(paths, next_attempt, provider, home=restored)
    except conversations.ConversationError:
        # A recoverable interrupted export may refuse resume until the
        # retained source is exported again (D-S4A.4 ordering).
        conversations.finish(paths, attempt, provider, 'ses_replacement', home=home)
        plan = conversations.prepare(paths, next_attempt, provider, home=restored)
    result = {p.name:p.read_text() for p in (restored / 'sessions').iterdir()}
    expected = {'ses_previous': {n:'previous-'+n for n in ('a.json','b.json')},
                'ses_replacement': {n:'replacement-'+n for n in ('a.json','b.json')}}
    assert result == expected[plan['session_id']], (plan, result)
