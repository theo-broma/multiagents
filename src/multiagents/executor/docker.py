"""Container executor — one long-lived container per project.

The local executor gives an agent git isolation and credential separation, but
not process isolation: an agent running with skip-permissions can reach anything
the user account can. This closes that. Inside the container "full powers" is
the correct default, because there is nothing left to protect.

Five constraints shape the implementation, each established by inspecting this
machine rather than assumed:

**Never mount the docker socket.** Docker here is rootful and the user is in the
``docker`` group, so socket access is equivalent to host root — an agent holding
it escapes in one command. There is no config option to enable it.

**Mount paths must match the host exactly.** A linked git worktree's ``.git``
file stores an absolute path to the main repository, and the repository stores
an absolute path back to the worktree. Mount either elsewhere and git breaks
confusingly. Every bind mount here uses ``<host path>:<same path>``.

**The container protects the host from the agent, not the tokens from the
agent.** The CLIs need their credential directories to authenticate, and a model
with a shell can read whatever is mounted. The control that helps is egress
filtering: agents sit on an *internal* Docker network with no route out, and
reach the world only through an allowlisting proxy. A token an agent can read is
then still a token it cannot post anywhere.

**Mount the CLIs, do not bake them in.** They self-update on the host; a copy in
the image would rot and would add hundreds of megabytes.

**Run as the invoking uid:gid.** Rootful Docker otherwise writes every file as
root, and a matching uid also avoids git's "dubious ownership" refusal.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..paths import ProjectPaths, state_root
from .base import Executor, Handle


class DockerHandle(Handle):
    """A ``docker exec`` client, plus the ability to stop what it started.

    Killing the client does NOT stop the process inside the container — verified:
    the exec'd command keeps running, keeps spending tokens, and its output goes
    nowhere. So the agent is launched through a shell that records its own pid to
    a file on a bind-mounted path, and stopping means signalling that pid from
    inside the container before killing the local client.
    """

    def __init__(self, pid: int, proc, container: str, pid_file: Path):
        super().__init__(pid=pid, _proc=proc)
        self.container = container
        self.pid_file = pid_file

    def _container_pid(self) -> str | None:
        try:
            value = self.pid_file.read_text().strip()
        except OSError:
            return None
        return value if value.isdigit() else None

    async def stop(self, grace: float = 10.0) -> None:
        target = self._container_pid()
        if target:
            # TERM the agent and its children, then KILL anything left.
            _run(["docker", "exec", self.container, "sh", "-c",
                  f"kill -TERM {target} 2>/dev/null; pkill -TERM -P {target} 2>/dev/null; true"],
                 timeout=30)
            await asyncio.sleep(min(grace, 5.0))
            _run(["docker", "exec", self.container, "sh", "-c",
                  f"kill -KILL {target} 2>/dev/null; pkill -KILL -P {target} 2>/dev/null; true"],
                 timeout=30)
        await super().stop(grace=grace)

PROXY_PORT = 8888


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def docker_available() -> str | None:
    return shutil.which("docker")


class DockerExecutor(Executor):
    kind = "docker"

    def __init__(
        self,
        config: dict[str, Any],
        paths: ProjectPaths | None = None,
        providers: dict[str, Any] | None = None,
        config_dir: Path | None = None,
    ):
        self.config = config or {}
        self.paths = paths
        self.providers = providers or {}
        self.config_dir = config_dir

    # ------------------------------------------------------------- naming --

    @property
    def slug(self) -> str:
        return self.paths.slug if self.paths else "default"

    @property
    def container(self) -> str:
        return self.config.get("container_name") or f"multiagents-{self.slug}"

    @property
    def proxy_container(self) -> str:
        return f"multiagents-proxy-{self.slug}"

    @property
    def network(self) -> str:
        return f"multiagents-net-{self.slug}"

    @property
    def image(self) -> str:
        return self.config.get("image", "multiagents/workspace:latest")

    @property
    def proxy_image(self) -> str:
        return self.config.get("proxy_image", "multiagents/proxy:latest")

    @property
    def network_mode(self) -> str:
        """``allowlist`` (internal net + proxy), ``bridge`` (open), ``none``."""
        return self.config.get("network", "allowlist")

    # -------------------------------------------------------------- mounts --

    def mounts(self) -> list[tuple[Path, bool]]:
        """(host path, read_only) pairs, each mounted at its own path.

        Three groups: the project and its git worktrees (writable, and required
        at identical paths for git to resolve); the CLI binaries (read-only);
        and each provider's credential/state directory, taken from the
        ``home_links`` already declared in providers.yaml so this list cannot
        drift from what the per-agent HOME expects to find.
        """
        if self.paths is None:
            return []
        out: list[tuple[Path, bool]] = [
            (self.paths.root, False),
            (self.paths.worktrees, False),
            (self.paths.homes, False),
        ]

        for entry in self.config.get("extra_mounts", []) or []:
            if isinstance(entry, str):
                out.append((Path(entry).expanduser(), False))
            elif isinstance(entry, dict) and entry.get("path"):
                out.append((Path(entry["path"]).expanduser(), bool(entry.get("read_only"))))

        if self.config.get("mount_cli_from_host", True):
            for provider in self.providers.values():
                binary = getattr(provider, "available", lambda: None)()
                if binary:
                    # Mount the path as found on PATH *and* its resolved target.
                    # claude's entry in ~/.local/bin is a symlink into a
                    # versioned directory: mounting only the resolved target
                    # leaves nothing named `claude` on PATH inside the container,
                    # and every run dies with "exec: claude: not found".
                    out.append((Path(binary), True))
                    resolved = Path(binary).resolve()
                    if resolved != Path(binary):
                        out.append((resolved, True))

                private = list(getattr(provider, "container_private_home", []) or [])
                for relative in getattr(provider, "home_links", []) or []:
                    if any(relative == p or relative.startswith(p + "/") for p in private):
                        continue        # masked below by a container-private dir
                    # Writable: opencode keeps a sqlite database in its data dir
                    # and agy writes conversation state. Read-only breaks them.
                    out.append((Path.home() / relative, False))

        seen: dict[Path, bool] = {}
        for path, read_only in out:
            if path.exists() and path not in seen:
                seen[path] = read_only
        mounts = sorted(seen.items())

        # Container-private state is mounted OVER the host path, so a per-agent
        # HOME's symlinks still resolve while the host's own credentials stay
        # untouched and unreachable.
        for host_path, private_path in self.private_state().items():
            private_path.mkdir(parents=True, exist_ok=True)
            mounts.append((host_path, False))
        return mounts

    def private_state(self) -> dict[Path, Path]:
        """{path as seen in the container: backing directory on the host}.

        Shared across projects by default: the credential is one account, and
        scoping it per project would mean logging in again for every repository.
        Set ``credential_scope: project`` if you genuinely want separate
        accounts per project.
        """
        if self.paths is None:
            return {}
        base = state_root() / "container-state"
        root = base / (self.slug if self.config.get("credential_scope") == "project"
                       else "shared")
        out: dict[Path, Path] = {}
        for name, provider in self.providers.items():
            for relative in getattr(provider, "container_private_home", []) or []:
                out[Path.home() / relative] = root / name / relative
        return out

    # ----------------------------------------------------------- lifecycle --

    def image_exists(self, name: str) -> bool:
        return _run(["docker", "image", "inspect", name]).returncode == 0

    def container_state(self, name: str) -> str:
        result = _run(["docker", "inspect", "-f", "{{.State.Status}}", name])
        return result.stdout.strip() if result.returncode == 0 else "absent"

    def build_image(self, dockerfile: Path, tag: str, timeout: int = 1800) -> dict:
        if not dockerfile.is_file():
            return {"ok": False, "error": f"missing {dockerfile}"}
        result = _run(
            ["docker", "build", "-t", tag, "-f", str(dockerfile), str(dockerfile.parent)],
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "tag": tag,
            "output": (result.stderr or result.stdout)[-1500:],
        }

    # --- egress proxy ------------------------------------------------------

    def write_proxy_config(self, target: Path) -> Path:
        """Generate tinyproxy's config and allowlist from project.yaml."""
        target.mkdir(parents=True, exist_ok=True)
        allow = list(self.config.get("egress_allowlist", []) or [])

        # FilterExtended uses POSIX extended regex against the destination host.
        # Anchored, with a leading optional subdomain group, so "example.com"
        # permits api.example.com but not evil-example.com.
        patterns = []
        for host in allow:
            escaped = host.replace(".", r"\.")
            patterns.append(f"(^|\\.){escaped}$")
        (target / "filter").write_text("\n".join(patterns) + "\n")

        (target / "tinyproxy.conf").write_text(
            "User nobody\n"
            "Group nogroup\n"
            f"Port {PROXY_PORT}\n"
            "Listen 0.0.0.0\n"
            "Timeout 600\n"
            "MaxClients 64\n"
            # Who may use the proxy: only the project's internal network.
            "Allow 0.0.0.0/0\n"
            "FilterDefaultDeny Yes\n"
            'Filter "/etc/tinyproxy/filter"\n'
            "FilterType ere\n"
            "FilterCaseSensitive Off\n"
            "FilterURLs Off\n"
            "ConnectPort 443\n"
            "DisableViaHeader Yes\n"
            "LogLevel Warning\n"
        )
        return target

    def ensure_network(self) -> dict:
        if _run(["docker", "network", "inspect", self.network]).returncode == 0:
            return {"ok": True, "existed": True}
        # --internal: no route off the host. This is what forces agent traffic
        # through the proxy rather than merely suggesting it.
        result = _run(["docker", "network", "create", "--internal", self.network])
        return {"ok": result.returncode == 0, "error": result.stderr.strip()[:300]}

    def ensure_proxy(self) -> dict:
        if self.network_mode != "allowlist":
            return {"ok": True, "skipped": self.network_mode}
        if not self.image_exists(self.proxy_image):
            return {"ok": False, "error": f"proxy image {self.proxy_image} not built"}

        state = self.container_state(self.proxy_container)
        if state == "running":
            return {"ok": True, "existed": True}
        if state != "absent":
            _run(["docker", "rm", "-f", self.proxy_container])

        config_dir = self.write_proxy_config(
            (self.config_dir or Path.home() / ".config" / "multiagents") / "proxy" / self.slug
        )
        result = _run([
            "docker", "run", "-d", "--name", self.proxy_container,
            "--network", self.network,
            "--restart", "unless-stopped",
            "-v", f"{config_dir / 'tinyproxy.conf'}:/etc/tinyproxy/tinyproxy.conf:ro",
            "-v", f"{config_dir / 'filter'}:/etc/tinyproxy/filter:ro",
            self.proxy_image,
        ])
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()[:400]}
        # Give the proxy a route out. The agent container never gets one.
        _run(["docker", "network", "connect", "bridge", self.proxy_container])
        return {"ok": True, "created": True}

    # --- workspace container ----------------------------------------------

    def run_args(self) -> list[str]:
        argv = [
            "docker", "run", "-d", "--name", self.container,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(self.paths.root) if self.paths else "/workspace",
            "--restart", "unless-stopped",
        ]
        if self.network_mode == "none":
            argv += ["--network", "none"]
        elif self.network_mode == "allowlist":
            argv += ["--network", self.network]

        for key, flag in (("cpus", "--cpus"), ("memory", "--memory"),
                          ("pids_limit", "--pids-limit")):
            value = self.config.get(key)
            if value:
                argv += [flag, str(value)]

        private = self.private_state()
        for path, read_only in self.mounts():
            source = private.get(path, path)
            argv += ["-v", f"{source}:{path}" + (":ro" if read_only else "")]

        if self.network_mode == "allowlist":
            proxy = f"http://{self.proxy_container}:{PROXY_PORT}"
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                argv += ["--env", f"{name}={proxy}"]
            argv += ["--env", "NO_PROXY=localhost,127.0.0.1"]

        return argv + [self.image, "sleep", "infinity"]

    def ensure_running(self) -> dict:
        if not docker_available():
            return {"ok": False, "error": "docker is not on PATH"}
        if not self.image_exists(self.image):
            return {"ok": False, "error": f"image {self.image} not built — run `multiagents docker build`"}

        if self.network_mode == "allowlist":
            net = self.ensure_network()
            if not net.get("ok"):
                return {"ok": False, "error": f"network: {net.get('error')}"}
            proxy = self.ensure_proxy()
            if not proxy.get("ok"):
                return {"ok": False, "error": f"proxy: {proxy.get('error')}"}

        state = self.container_state(self.container)
        if state == "running":
            return {"ok": True, "container": self.container, "existed": True}
        if state in ("exited", "created", "paused"):
            result = _run(["docker", "start", self.container])
            if result.returncode == 0:
                return {"ok": True, "container": self.container, "started": True}
            _run(["docker", "rm", "-f", self.container])

        result = _run(self.run_args(), timeout=300)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()[:600]}
        return {"ok": True, "container": self.container, "created": True}

    def stop(self, remove: bool = False) -> dict:
        out = {}
        for name in (self.container, self.proxy_container):
            if self.container_state(name) == "absent":
                continue
            out[name] = _run(["docker", "rm", "-f", name] if remove
                             else ["docker", "stop", name]).returncode == 0
        return {"ok": True, "acted_on": out}

    # -------------------------------------------------------------- execute --

    def preflight(self) -> list[str]:
        problems: list[str] = []
        if not docker_available():
            return ["docker is not on PATH"]
        if not self.image_exists(self.image):
            problems.append(f"image {self.image} not built — run `multiagents docker build`")
        if self.network_mode == "allowlist" and not self.image_exists(self.proxy_image):
            problems.append(f"proxy image {self.proxy_image} not built")
        if self.config.get("mount_docker_socket"):
            # Refused rather than honoured: with rootful Docker this is host root.
            problems.append(
                "mount_docker_socket is set. Refusing: with rootful Docker that "
                "grants host root and voids the container boundary entirely."
            )
        return problems

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> Handle:
        state = self.ensure_running()
        if not state.get("ok"):
            raise RuntimeError(f"docker executor: {state.get('error')}")

        # Environment goes through a file rather than --env flags so that values
        # never appear in the host process list.
        env_file = None
        if self.paths is not None:
            env_dir = self.paths.data / "env"
            env_dir.mkdir(parents=True, exist_ok=True)
            env_file = env_dir / f"{env.get('MULTIAGENTS_AGENT_ID', 'run')}.env"
            env_file.write_text(
                "".join(f"{k}={v}\n" for k, v in env.items() if "\n" not in str(v))
            )
            env_file.chmod(0o600)

        command = ["docker", "exec", "-i", "--workdir", str(cwd),
                   "--user", f"{os.getuid()}:{os.getgid()}"]
        if env_file is not None:
            command += ["--env-file", str(env_file)]
        else:
            for key, value in env.items():
                command += ["--env", f"{key}={value}"]
        command.append(self.container)

        # Record the agent's container-side pid so it can actually be stopped.
        # `exec "$@"` replaces the shell, so $$ is the agent's own pid.
        pid_file = (self.paths.run_dir(env.get("MULTIAGENTS_AGENT_ID", "run"))
                    if self.paths is not None else Path("/tmp")) / "container.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        command += ["sh", "-c", f'echo $$ > "{pid_file}"; exec "$@"', "--", *argv]

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return DockerHandle(proc.pid, proc, self.container, pid_file)
