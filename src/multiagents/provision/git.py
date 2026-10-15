"""Git commands for host-owned provisioning and quarantine repositories."""
from __future__ import annotations

import os


def git_env() -> dict[str, str]:
    """Discard ambient Git configuration, object stores and executable helpers."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_LFS_SKIP_SMUDGE="1", GIT_ATTR_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    return env


def git_command(*args: str) -> list[str]:
    return ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "init.templateDir=", "-c", "core.attributesFile=/dev/null",
            "-c", "submodule.recurse=false", "-c", "protocol.ext.allow=never", *args]
