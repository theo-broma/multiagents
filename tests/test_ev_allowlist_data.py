"""EV-R1 (F112): the packaged allowlist matches the named contract inputs."""

from multiagents.scripts import SCRIPT_ENV_KEYS


def test_ev_r1_loaded_allowlist_matches_contract_exactly():
    assert SCRIPT_ENV_KEYS == frozenset({
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "TMPDIR",
        "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "MULTIAGENTS_CODEX_PROFILE", "MULTIAGENTS_OPENCODE_PLAN", "MULTIAGENTS_ZAI_ORIGIN",
    })
