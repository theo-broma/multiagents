"""FC1/F136 and FC2/F137: authentication signals through the public classifier."""
import pytest
from multiagents.auth import looks_like_auth_failure


@pytest.mark.parametrize('message', [
    '40105', '401 items', 'v4.0.1', 'id_401', 'took 401ms', 'HTTP 401ms',
    'exit code 401', 'compile error 401', 'code 401', 'error 401',
    'HTTP 4010', 'HTTP 401A', 'HTTP 401.0', 'HTTP 401_',
    'status: 1401', 'status: .401', 'status: _401', '', '   ',
])
@pytest.mark.parametrize('channel', ['status', 'stderr'])
def test_fc1_rejects_401_without_http_context_or_at_invalid_boundaries(message, channel):
    assert not looks_like_auth_failure(message if channel == 'status' else '',
                                       message if channel == 'stderr' else '')


@pytest.mark.parametrize('message', [
    'HTTP 401', 'HTTP/1.1 401', 'HTTPError: 401', 'status: 401',
    'status_code=401', '"status": 401', '401 Unauthorized',
    'hTtP/2 401', 'STATUS CODE = "401"', '{"status_code":401}',
    'HTTP:401;', 'status=401)', 'unauthorized',
])
def test_fc1_accepts_http_status_and_standalone_unauthorized(message):
    assert looks_like_auth_failure('', message)


@pytest.mark.parametrize('phrase', [
    'session expired', 'session has expired', 'sign in again',
    'api key missing', 'missing api key',
])
@pytest.mark.parametrize('channel', ['status', 'stderr'])
@pytest.mark.parametrize('uppercase', [False, True])
def test_fc2_recognizes_each_new_phrase_in_both_channels(phrase, channel, uppercase):
    message = phrase.upper() if uppercase else phrase
    assert looks_like_auth_failure(message if channel == 'status' else '',
                                   message if channel == 'stderr' else '')


@pytest.mark.parametrize('veto', ['permission denied', 'auto-denied', 'dangerously-skip'])
@pytest.mark.parametrize('phrase', ['sign in again', 'session expired', 'api key missing'])
def test_fc2_login_words_override_permission_veto(veto, phrase):
    assert looks_like_auth_failure('failed', f'{veto}; {phrase}')


@pytest.mark.parametrize('message', [
    'permission denied', 'Tool use was auto-denied due to permission settings',
    'dangerously-skip-permissions is not allowed here',
    'quota exceeded; rate limit exceeded; retry later',
])
def test_fc2_keeps_permission_and_quota_messages_non_auth(message):
    assert not looks_like_auth_failure('failed', message)


@pytest.mark.parametrize('phrase', [
    'authentication required', 'not authenticated', 'unauthenticated',
    'please log in', 'please login', "run 'agy' to log in", 'log in, then retry',
    'providers login', 'auth login', 'invalid api key', 'invalid_api_key',
    'unauthorized', 'authentication failed', 'credentials not found',
    'no credentials', 'token expired', 'oauth token',
])
def test_fc2_preserves_existing_auth_phrases(phrase):
    assert looks_like_auth_failure('', phrase)
