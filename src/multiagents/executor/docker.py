"""Container executor — phase 2, not yet implemented.

This file exists so the target shape is visible while the local backend is in
use, and so the interface it must satisfy stays honest. The constraints below
were established by inspecting this machine and are not negotiable details:

**Never mount the docker socket.** Docker here is rootful and the user is in the
``docker`` group, so socket access is equivalent to host root — an agent holding
it escapes the container in one command.

**Mount paths must match the host exactly.** A linked git worktree's ``.git``
file stores an absolute path to the main repository, and the repository stores
an absolute path back to the worktree. Mount either at a different path inside
the container and git breaks in ways that are tedious to diagnose.

**A container does not protect credentials from the agent.** It protects the
*host* from the agent. Anything mounted so a CLI can authenticate can also be
read by a model with a shell. The control that actually helps is egress
allowlisting: give the container no direct route out and force traffic through a
CONNECT proxy that permits only the model endpoints, so a token an agent can
read is a token it cannot post anywhere. That proxy is also the natural place to
later inject ``Authorization`` headers so tokens never enter the container.

**Mount the CLIs, do not bake them in.** opencode alone is a 177 MB
self-updating binary; bind-mounting the host's copies read-only keeps the image
small and keeps updates working the way they already do.

**Fix ownership.** Rootful Docker plus a default user means every file the agent
creates lands as root. Run as the invoking uid/gid with a writable home.
"""

from __future__ import annotations

from pathlib import Path

from .base import Executor, Handle


class DockerExecutor(Executor):
    kind = "docker"

    def __init__(self, config: dict):
        self.config = config

    def preflight(self) -> list[str]:
        return ["the docker executor is not implemented yet; set executor.kind: local"]

    def plan(self, argv: list[str], cwd: Path, env: dict[str, str]) -> list[str]:
        """The command this executor will eventually run.

        Implemented ahead of ``start`` so the design is inspectable and
        testable — ``multiagents doctor`` prints it for review.
        """
        cfg = self.config
        container = cfg.get("container_name", "multiagents-<project-slug>")
        command = ["docker", "exec", "--workdir", str(cwd)]
        for key, value in env.items():
            command += ["--env", f"{key}={value}"]
        command.append(container)
        return command + argv

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> Handle:
        raise NotImplementedError(
            "The docker executor is not implemented yet. Set executor.kind to "
            "'local' in .multiagents/config/project.yaml. See the module "
            "docstring for the constraints the implementation must satisfy."
        )
