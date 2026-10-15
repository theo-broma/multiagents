"""IC-C22: invalid session declarations fail through the public provider loader."""
import pytest

from multiagents.providers import load_providers


@pytest.mark.parametrize("session,field", [
    ({"mode": "unknown"}, "mode"),
    ({"mode": "home"}, "path"),
    ({"mode": "export"}, "path"),
    ({"mode": "home", "path": "/sessions"}, "path"),
    ({"mode": "export", "path": "/sessions"}, "path"),
    ({"mode": "home", "path": "a/../sessions"}, "path"),
    ({"mode": "export", "path": "../sessions"}, "path"),
    ("home", "session"),
    ([], "session"),
    ({"mode": "home", "path": "sessions", "resume_deadline_seconds": 0},
     "resume_deadline_seconds"),
    ({"mode": "export", "path": "sessions", "resume_deadline_seconds": -1},
     "resume_deadline_seconds"),
], ids=["unknown-mode", "home-missing-path", "export-missing-path", "home-absolute",
        "export-absolute", "home-parent", "export-parent", "scalar", "list",
        "zero-deadline", "negative-deadline"])
def test_ic_c22_rejects_invalid_session_with_provider_and_field(session, field):
    with pytest.raises(ValueError) as error:
        load_providers({"session-probe": {"bin": "python3", "session": session}})
    assert "session-probe" in str(error.value)
    assert field in str(error.value)
