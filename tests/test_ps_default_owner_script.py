"""PS1 / F113: renaming a borrowed account preserves its default script."""
import subprocess
from types import SimpleNamespace

import pytest

from multiagents import auth
from multiagents.providers import load_providers


@pytest.mark.parametrize("action,pin", [("check", ""), ("check", "account-b"),
                                       ("login", "account-b")])
def test_borrowed_account_keeps_owners_implicit_script(tmp_path, action, pin):
    folder = tmp_path / "providers"
    folder.mkdir()
    script = folder / "owner.sh"
    script.write_text('printf "%s|%s\\n" "$MULTIAGENTS_PROVIDER" "$OWNER_CREDENTIAL"\n')
    providers = load_providers({
        "owner": {"bin": "sh", "env": {"OWNER_CREDENTIAL": "borrowed"}},
        "instance": {"bin": "sh", "auth_from": "owner", "container_account": pin},
    })
    executor = SimpleNamespace(kind="docker", container="test", private_state=lambda name: {})
    if action == "check":
        state = auth.check("instance", providers["instance"], executor, tmp_path)
        assert state.ok and state.script == str(script)
        assert state.detail == "instance|borrowed"
    else:
        argv, env = auth.login_command("instance", providers["instance"], executor, tmp_path)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=3)
        assert result.returncode == 0 and result.stdout == "instance|borrowed\n"
    assert providers["owner"].name == "owner"
    assert providers["owner"].script == ""
