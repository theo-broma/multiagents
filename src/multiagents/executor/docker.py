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
import contextlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
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

# The proxy's filter lines are POSIX EREs (tinyproxy is built with
# `FilterType ere`), not Python regexes. An allowlist entry must become a
# literal in *that* dialect, so we escape exactly the characters ERE gives
# special meaning to outside a bracket expression: `\ ^ $ . | ? * + ( ) [ ] { }`.
# `re.escape` is the wrong tool here — it also escapes characters such as
# `-`, `&`, `~`, `#` and space that ERE does not treat as special, and POSIX
# leaves a backslash before an otherwise-ordinary character undefined. glibc's
# regcomp happens to read that as the literal character, but "happens to" is
# not a guarantee, and a hostname is exactly the kind of string likely to
# contain a `-`.
_ERE_ESCAPE_TABLE = {ord(c): "\\" + c for c in r"\^$.|?*+()[]{}"}


def _ere_literal(text: str) -> str:
    """Escape `text` so it matches only itself as a POSIX ERE.

    `str.translate` so a non-string entry still fails with the same
    `AttributeError` the old `host.replace(...)` gave — a malformed entry is
    F2/F55, out of scope for this fix, and not something to change the shape
    of as a side effect.
    """
    return text.translate(_ERE_ESCAPE_TABLE)


def _is_ipv4_literal(text: str) -> bool:
    """True when `text` is a dotted-decimal IPv4 address (four octets, 0–255)."""
    try:
        ipaddress.IPv4Address(text)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _is_ipv6_literal(text: str) -> bool:
    """True when `text` is an IPv6 address, bare or bracket-wrapped."""
    inner = text
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    try:
        ipaddress.IPv6Address(inner)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


# A valid hostname label: starts and ends with an alnum, interior may contain
# hyphens, 1–63 characters.  All-numeric labels are accepted so that IPv4
# addresses (handled separately) are not rejected by the grammar check.
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate_allowlist_entry(entry: str) -> str | None:
    """Return a human-readable problem message, or ``None`` if `entry` is valid.

    Checked in the order the test vocabulary requires — each earlier check
    prevents a later, less-specific one from claiming the entry.  The contract
    says ``write_proxy_config`` must NOT gain a raise; this function is called
    from ``preflight()`` only.
    """
    # 1. Empty (includes whitespace-only after strip — but whitespace-only
    #    strings with non-empty pre-strip text are caught by step 2 instead).
    if not entry.strip():
        if not entry:
            return "empty allowlist entry"
        # Whitespace-only: still empty after stripping.
        return f"allowlist entry is blank (whitespace only: {entry!r})"

    # 2. Leading/trailing whitespace on a non-empty entry.
    if entry != entry.strip():
        return (
            f"allowlist entry {entry.strip()!r} has surrounding whitespace — "
            f"did you mean {entry.strip()!r}?"
        )

    # 3. Leading or trailing dot.
    if entry.startswith("."):
        return f"allowlist entry {entry!r} has a leading dot"
    if entry.endswith("."):
        return f"allowlist entry {entry!r} has a trailing dot — remove the period"

    # 4. IPv6 literal (bare or bracketed).  Must come before the port check so
    #    that `::1` says "IPv6 address" rather than "looks like a port".
    if _is_ipv6_literal(entry):
        return (
            f"allowlist entry {entry!r} is an IPv6 address, which tinyproxy's "
            f"host filter cannot express — use the hostname instead"
        )

    # 5. URL with scheme.
    if "://" in entry:
        return (
            f"allowlist entry {entry!r} looks like a URL — use the bare "
            f"hostname without a scheme or path"
        )

    # 6. Valid IPv4 literal — accept it.
    if _is_ipv4_literal(entry):
        return None

    # 7. Host:port shape.
    if re.search(r":\d+$", entry):
        return (
            f"allowlist entry {entry!r} has a port suffix — tinyproxy matches "
            f"the bare hostname, not a host:port pair"
        )

    # 8. Hostname grammar: split on dots, check each label.
    labels = entry.split(".")
    for label in labels:
        if not _LABEL_RE.match(label):
            return f"allowlist entry {entry!r} is not a valid hostname"

    # All labels passed — valid.
    return None


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def docker_available() -> str | None:
    return shutil.which("docker")


def list_containers(include_stopped: bool = True) -> list[dict]:
    """Every multiagents container on this machine, whatever project made it.

    Each project has two — the workspace and its filtering proxy — and the name
    carries the project slug, which is what lets a cross-project view exist at
    all.
    """
    argv = ["docker", "ps", "--filter", "name=multiagents-",
            "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.RunningFor}}"]
    if include_stopped:
        argv.insert(2, "-a")
    result = _run(argv, timeout=20)
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name = parts[0]
        proxy = name.startswith("multiagents-proxy-")
        slug = name[len("multiagents-proxy-"):] if proxy else name[len("multiagents-"):]
        rows.append({"name": name, "slug": slug, "proxy": proxy,
                     "status": parts[1], "image": parts[2],
                     "age": parts[3] if len(parts) > 3 else ""})
    return rows


def docker_state() -> tuple[str, str]:
    """`(state, detail)` where state is ok | no-binary | no-daemon.

    The binary being on PATH is not the same as being able to use it — a
    stopped daemon and a user outside the `docker` group both look like a
    working install until the first command fails.
    """
    if not shutil.which("docker"):
        return "no-binary", "docker is not installed"
    probe = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    if probe.returncode == 0:
        return "ok", (probe.stdout.strip() or "running")
    detail = (probe.stderr or probe.stdout).strip().splitlines()
    reason = detail[-1][:160] if detail else "daemon unreachable"
    return "no-daemon", reason


# One stdout line can carry a whole file: a CLI reports a tool result as a single
# JSON object, and reading a 60 KB source file makes a 60 KB line. asyncio's
# default StreamReader limit is 64 KiB, and exceeding it raises ValueError from
# readline() and kills the run — which is what stopped the bug-reporter every
# time, its brief being to read this project's own source.
STREAM_LIMIT = 16 * 1024 * 1024


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
    def auth_container(self) -> str:
        return f"multiagents-auth-{self.slug}"

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

    @staticmethod
    def _versions_dir(launcher: Path, resolved: Path) -> Path | None:
        """The versions directory backing a versioned launcher, or ``None``.

        A versioned launcher (P0-R1, `F200`) is a symlink whose resolved
        target is a FILE in a directory other than the launcher's own — that
        directory counts as the versions directory whatever its name, even
        holding a single entry. A target nested below a per-version directory
        (``versions/1.0.0/bin/x``) is out of scope: it reads as unversioned
        here and keeps today's behaviour.
        """
        if resolved == launcher or not resolved.is_file():
            return None
        if resolved.parent == launcher.parent:
            return None
        return resolved.parent

    def _resolve_launcher(self, binary: str) -> Path:
        """Where `binary`'s host launcher points RIGHT NOW.

        Docker fixes a bind mount's target at container creation; a versioned
        launcher's target moves after that. Resolving on the host at every
        spawn — rather than trusting whatever `mounts()` last computed — is
        what makes the container run the CLI version current at spawn time
        without being recreated (P0-R1.2).
        """
        return Path(binary).resolve()

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
                    launcher_path = Path(binary)
                    out.append((launcher_path, True))
                    resolved = launcher_path.resolve()
                    versions_dir = self._versions_dir(launcher_path, resolved)
                    if versions_dir is not None:
                        # A versioned launcher: mount the versions directory
                        # itself, not the resolved file, so the declared mount
                        # list is stable across a CLI update (P0-R1.1) and the
                        # container can still reach whatever version the host
                        # launcher points at after the update (P0-R1.2) without
                        # being recreated.
                        out.append((versions_dir, True))
                    elif resolved != launcher_path:
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

        # Same trick, for the opposite reason: the project root is mounted
        # writable because agents commit in it, and `.multiagents/config` sits
        # inside it — so `project.yaml` was writable by every agent in here.
        # That file IS this container's boundary: egress_allowlist, extra_mounts,
        # mount_docker_socket. An agent could widen its own sandbox.
        #
        # Not an immediate escape, because none of it takes effect until the
        # container restarts and nothing in here can restart it. That makes it
        # worse to reason about rather than better: the damage lands on a later
        # run, under a user who did not make the change and has no reason to
        # re-read a config file they already wrote.
        #
        # Mounted read-only OVER the writable root, after it, so the narrower
        # mount wins. The rest of `.multiagents` — run dirs, the event stream,
        # tree.json — stays writable, because agents genuinely do write there.
        if self.paths.config.is_dir():
            mounts.append((self.paths.config, True))
        return mounts

    # Keys never carried into a container-private profile. `env` and an
    # api-key helper are how a settings file hands out credentials, and this
    # project's whole environment policy is that agents get none: passing them
    # in through a config copy would be the same leak by a quieter route.
    #
    # A fixed list of names is not enough on its own — an advisor's objection,
    # and a fair one: the vendor adds a key, this list does not know it, and a
    # secret rides along. So the names below are the floor, and every remaining
    # value is also judged by the same redactor that guards everything written
    # to disk, which recognises secret-SHAPED strings and secret-NAMED keys
    # whatever the schema does next.
    UNSAFE_SETTINGS = ("env", "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")

    def seed_private_state(self) -> list[str]:
        """Carry the user's own configuration into a container-private profile.

        A private profile fixes the credential, and would otherwise amputate
        everything else the user had configured: permissions, hooks, model
        choice, plugins. The agent would run as a factory-reset CLI and nobody
        would connect that to a credential change.

        Copied, not linked, because these files are edited by hand once in a
        while rather than rewritten by a process — the opposite of the
        credential, and the reason copying is safe here and wrong there.
        """
        notes = []
        for name, provider in self.providers.items():
            backing = self.private_state(name)
            if not backing:
                continue
            backing_root = next(iter(backing.values()))
            for relative in getattr(provider, "container_private_seed", []) or []:
                source = Path.home() / relative
                target = backing_root / Path(relative).name
                if not source.is_file():
                    continue
                # Seeded ONCE, not kept in step. "Copy when the host's is
                # newer" reads well and clobbers: edit the container's copy to
                # fix something container-specific, add an unrelated line to
                # the host's a month later, and the fix is silently gone. To
                # re-seed, delete the copy.
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stripped = self._copy_settings(source, target)
                if stripped:
                    notes.append(f"{name}: copied {Path(relative).name} into the "
                                 f"container profile without {', '.join(stripped)} "
                                 f"— agents are not given credentials through config")
            # Host-pid state means nothing in a container and confuses the CLI
            # that finds it: a lock naming a pid it cannot signal.
            #
            # Only when the process is actually gone. Deleting a lock a live
            # daemon still holds does not stop the daemon — it lets a second
            # one start alongside it, and then two of them share one state
            # directory, which is a worse problem than the one being fixed.
            for relative in getattr(provider, "container_private_reset", []) or []:
                stale = backing_root / Path(relative).name
                if not stale.exists():
                    continue
                if self._holder_alive(stale):
                    notes.append(f"{name}: {stale.name} is held by a live "
                                 f"process; left alone")
                    continue
                if stale.is_dir():
                    shutil.rmtree(stale, ignore_errors=True)
                else:
                    stale.unlink(missing_ok=True)
        return notes

    # How stale the container profile's token may be before a spawn renews it.
    # Renewed EARLY, not on expiry: an agent that starts with four minutes left
    # gets a 401 partway through a run it has already paid for, and a run that
    # dies mid-turn costs far more than a refresh that was not strictly due.
    REFRESH_MARGIN = 30 * 60

    def refresh_private_credentials(self) -> list[str]:
        """Renew a container-private token that the container cannot renew.

        The container has no route to the refresh endpoint and giving it one
        means handing every agent a host that also serves account settings. It
        does not need one: this runs on the HOST, through the provider's own
        script, and the container reads the file afterwards.

        Called from `ensure_running`, which is on the path of every spawn, so
        the check has to be cheap. It is a file read; the network call happens
        only inside the margin above, which is at most once per token lifetime.
        """
        from .. import scripts
        from ..paths import global_config_dir

        notes = []
        for name, provider in self.providers.items():
            backing = self.private_state(name)
            if not backing:
                continue
            # The VAULT's clock, never the projection's. The projection sits
            # in a directory the container writes to, so an agent can put any
            # expiry it likes in there: 1970 to make this refresh in a loop
            # until the account is rate-limited, 2099 to stop it refreshing at
            # all and strand every later agent. The host must not take state
            # from a file the sandbox can edit.
            #
            # Before the vault exists — a profile from an older install, or the
            # first spawn after upgrading — the projection IS the credential
            # and reading it is all there is. The script migrates on its first
            # run, so that window is one spawn wide.
            root = next(iter(backing.values()))
            vault = self.vault_state(name).get(name)
            clock = vault / ".credentials.json" if vault and \
                (vault / ".credentials.json").is_file() else root / ".credentials.json"
            if not self._expiring_soon(clock):
                continue
            with self._refresh_lock(name) as held:
                # Somebody else got there first. Not worth waiting for: the
                # margin means the token is still good for half an hour, so
                # this spawn proceeds on it and the other process's result
                # lands long before it matters.
                if not held:
                    continue
                # Re-read inside the lock. Between the check above and the lock
                # the other process may have finished, and a second renewal
                # would be a wasted call at best — and at worst, if this
                # provider rotates refresh tokens, two renewals racing on one
                # token is how a provider decides it has been stolen.
                if not self._expiring_soon(clock):
                    continue
                code, out, err = scripts.run_action(
                    name, provider, self, "refresh", global_config_dir(),
                    self.paths.config if self.paths else None, timeout=120)
            line = (out.strip() or err.strip()).splitlines()
            if code == scripts.UNIMPLEMENTED:
                continue                  # provider has no container profile
            notes.append(f"{name}: {line[-1][:200] if line else f'refresh exit {code}'}")
        return notes

    @contextlib.contextmanager
    def _refresh_lock(self, provider: str):
        """Serialise renewals on OUR side, non-blocking. Yields whether held.

        The vendor ships a lock for this and it is the reason any of it was
        found: a bare mkdir mutex, an empty directory naming no owner, which
        cannot be asked whether its holder is alive and which a process that
        dies mid-refresh leaves behind forever. Depending on it to serialise
        our own concurrency — several agents can spawn at once, and each spawn
        passes through here — would be building on the thing that broke.
        """
        import fcntl

        # Beside the credential it guards, which is shared across projects by
        # default, so a second project spawning at the same moment contends on
        # the same lock rather than racing it.
        path = state_root() / "container-state" / f"{provider}.refresh.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = path.open("w")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True
        except OSError:
            # A lock we cannot take is not a reason to refuse to spawn; it is a
            # reason not to be the one refreshing.
            yield False
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @classmethod
    def _expiring_soon(cls, credentials: Path) -> bool:
        """Is this stored token inside the renewal margin? Never raises.

        Unreadable or unfamiliar reads as "no", because the alternative is
        forcing a network call on every single spawn for a file shape we do
        not recognise.
        """
        import json as _json
        import time as _time

        try:
            data = _json.loads(credentials.read_text())
        except (OSError, ValueError):
            return False
        for block in data.values():
            if isinstance(block, dict) and block.get("expiresAt"):
                return int(block["expiresAt"]) / 1000 - _time.time() < cls.REFRESH_MARGIN
        return False

    @staticmethod
    def _holder_alive(lock: Path) -> bool:
        """Does a pid named inside this file still exist on this host?"""
        import re as _re

        try:
            text = lock.read_text(errors="replace")[:4096] if lock.is_file() else ""
        except OSError:
            return True                       # unreadable: assume it is in use
        match = _re.search(r'"?pid"?\s*[:=]\s*"?(\d+)', text)
        if not match:
            return False
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _copy_settings(self, source: Path, target: Path) -> list[str]:
        """Copy a config file, dropping any key that carries a secret."""
        from ..redact import scrub

        # A target that is a symlink would be followed, and the write would
        # land on whatever it points at — including, if somebody linked it
        # back, the user's own file. Replace the link, never write through it.
        if target.is_symlink():
            target.unlink()
        try:
            data = json.loads(source.read_text())
        except (OSError, ValueError):
            shutil.copy2(source, target, follow_symlinks=False)
            return []
        if not isinstance(data, dict):
            shutil.copy2(source, target, follow_symlinks=False)
            return []

        stripped = [key for key in self.UNSAFE_SETTINGS if key in data]
        for key in stripped:
            data.pop(key, None)
        masked = scrub(data)
        if masked != data:
            stripped.append("values that look like secrets")
        target.write_text(json.dumps(masked, indent=2))
        return stripped

    def mount_drift(self) -> list[str]:
        """Mounts the running container has that the configuration no longer wants.

        Bind mounts are fixed when a container is CREATED. Stopping and starting
        it re-resolves each source path — which is why a restart cures inode
        drift — but the SET of mounts is whatever was decided at creation, so a
        configuration change reaches a long-lived container only when it is
        replaced.

        Measured cost of not saying so: a project ran for three days against a
        container created before its credential layout changed, with every fix
        shipped, tested, believed in, and not actually in effect. `down` and
        `up` do not do it; `rm` and `up` do.
        """
        if self.container_state(self.container) != "running":
            return []
        result = _run(["docker", "inspect", "-f",
                       "{{range .Mounts}}{{.Source}}>{{.Destination}}\n{{end}}",
                       self.container])
        if result.returncode != 0:
            return []
        have = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        private = self.private_state()
        want = {f"{private.get(path, path)}>{path}" for path, _ in self.mounts()}
        missing = sorted(want - have)
        extra = sorted(h for h in have - want if h.split(">")[1] in
                       {str(p) for p, _ in self.mounts()} | {str(p) for p in private})
        out = [f"missing: {m}" for m in missing]
        out += [f"stale:   {e}" for e in extra]
        return out

    def credential_drift(self) -> list[dict]:
        """Bind-mounted credential files the container no longer shares with us.

        Docker binds a FILE by its inode. Every CLI here writes a credential the
        safe way — new file, then rename over the old path — which produces a
        NEW inode, so the host path moves on and the container keeps the old
        one, now unlinked, forever. The two stop being the same file and nobody
        is told.

        Measured cost of not noticing: a container bound at 00:25 kept serving a
        token from the night before. The host refreshed at 15:51, the old
        refresh token was rotated away and therefore revoked, and from then on
        every agent in that container failed with "401 OAuth access token has
        been revoked" while `auth status` on the host read the correct file and
        said everything was fine. A whole day of runs.

        Comparing inodes settles it in one `docker exec`, and a restart — not a
        rebuild — re-resolves the bind.
        """
        if self.container_state(self.container) != "running":
            return []
        private = {str(path) for path in self.private_state()}
        wanted: list[Path] = []
        for provider in self.providers.values():
            for relative in getattr(provider, "home_links", []) or []:
                candidate = Path.home() / relative
                if candidate.is_file() and not any(
                        str(candidate).startswith(prefix) for prefix in private):
                    wanted.append(candidate)
        if not wanted:
            return []

        script = "; ".join(f'stat -c "%i" {path} 2>/dev/null || echo -' 
                           for path in wanted)
        result = _run(["docker", "exec", self.container, "sh", "-c", script])
        if result.returncode != 0:
            return []
        inside = result.stdout.split()
        out = []
        for path, seen in zip(wanted, inside):
            try:
                host = str(path.stat().st_ino)
            except OSError:
                continue
            if seen not in ("-", host):
                out.append({"path": str(path), "host_inode": host,
                            "container_inode": seen})
        return out

    def private_state(self, provider: str = "") -> dict[Path, Path]:
        """{path as seen in the container: backing directory on the host}.

        Filtered by provider when asked. It used to be all-or-nothing, and the
        one caller that wanted a single provider's backing path took whichever
        entry came first — correct only while exactly one provider had a
        private home, and silently wrong the moment a second did.

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
        for name, entry in self.providers.items():
            if provider and name != provider:
                continue
            for relative in getattr(entry, "container_private_home", []) or []:
                out[Path.home() / relative] = root / name / relative
        return out

    def vault_state(self, provider: str = "") -> dict[str, Path]:
        """{provider: the host-only profile holding its REAL credential}.

        A sibling of the mounted profile and deliberately NOT in `mounts()`, so
        nothing inside the container can reach it. The refresh token lives here
        and only here; what the container gets is a projection carrying the
        eight-hour access token and nothing else.

        The point is the blast radius. A credential an agent can read is one it
        can copy out, and agents run with approvals off — so the question is
        not whether one could take it but how long a stolen one is worth
        having. Twenty-eight days of account access is persistence; eight hours
        is a window that closes on its own.
        """
        if self.paths is None:
            return {}
        base = state_root() / "container-state"
        root = base / (self.slug if self.config.get("credential_scope") == "project"
                       else "shared")
        out = {}
        for name, entry in self.providers.items():
            if provider and name != provider:
                continue
            if getattr(entry, "container_private_home", None):
                out[name] = root / name / "vault"
        return out

    # ----------------------------------------------------------- lifecycle --

    def image_exists(self, name: str) -> bool:
        return _run(["docker", "image", "inspect", name]).returncode == 0

    def started_at(self, name: str = "") -> float | None:
        """When the container started, as a unix time. None if it is not up."""
        import datetime

        result = _run(["docker", "inspect", "-f", "{{.State.StartedAt}}",
                       name or self.container])
        if result.returncode != 0:
            return None
        stamp = result.stdout.strip()
        try:
            # Docker returns RFC3339 with nanoseconds, which fromisoformat
            # rejects before 3.11 and dislikes with a Z suffix.
            stamp = stamp.replace("Z", "+00:00")
            head, _, rest = stamp.partition(".")
            if rest:
                frac, _, tz = rest.partition("+")
                stamp = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
            return datetime.datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            return None

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
            escaped = _ere_literal(host)
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

    # The proxy speaks one upstream's protocol: it forwards to
    # api.anthropic.com and swaps an Anthropic bearer header. So it serves one
    # provider, and naming it here is more honest than taking whichever
    # provider's vault happened to come first out of a dict — which is what
    # this did on its first run, mounting agy's vault into a proxy that talks
    # to Anthropic and writing an Anthropic-shaped credential into agy's
    # profile. A second provider needs its own upstream, not a share of this.
    AUTH_PROVIDER = "claude"

    def auth_proxy_enabled(self) -> bool:
        """Off unless asked for, and only where there is a vault to hold.

        It changes where every agent's model traffic goes, so it is not
        something to acquire by upgrading.
        """
        return bool(self.config.get("auth_proxy")) and \
            bool(self.vault_state(self.AUTH_PROVIDER))

    def ensure_auth_proxy(self) -> dict:
        """The only thing on the agents' network that can reach the model API.

        Same shape as the egress proxy beside it, for the same reason: a
        sidecar on the internal network, given a route out that the workspace
        container does not have. The vault is mounted READ-ONLY and into THIS
        container — which agents have no more access to than they have to the
        host — so the credential is on the path of every request and inside
        none of the places agents can read.
        """
        if not self.auth_proxy_enabled():
            return {"ok": True, "skipped": "not enabled"}
        vault = self.vault_state(self.AUTH_PROVIDER)[self.AUTH_PROVIDER]
        vault.mkdir(parents=True, exist_ok=True)
        # The secret must exist before the container reads it, and the host
        # mints agents' tags from the same file.
        from ..authproxy import PORT, load_secret
        load_secret(vault)

        if self.container_state(self.auth_container) == "running":
            return {"ok": True, "existed": True}
        _run(["docker", "rm", "-f", self.auth_container])
        # Neutral paths inside, unlike everywhere else here. The identical-path
        # rule exists so git can resolve a worktree; this container has no
        # worktree and no git, and mounting the package over its own deep host
        # path only invites the parent directories to be created by docker and
        # owned by root.
        source = Path(__file__).resolve().parent.parent     # .../multiagents
        result = _run([
            "docker", "run", "-d", "--name", self.auth_container,
            "--network", self.network,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--restart", "unless-stopped",
            "-v", f"{vault}:/vault:ro",
            "-v", f"{source}:/opt/ma/multiagents:ro",
            "--env", "PYTHONPATH=/opt/ma",
            self.image,
            "python3", "-m", "multiagents.authproxy", "/vault", "0.0.0.0", str(PORT),
        ])
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()[:400]}
        _run(["docker", "network", "connect", "bridge", self.auth_container])
        return {"ok": True, "created": True}

    def project_placeholder(self) -> None:
        """Replace the container's credential with a name-tag.

        One tag per CONTAINER, not per agent, and that is the right grain for
        two reasons. Agents in a container share one profile — their per-agent
        HOMEs symlink to it — so a per-agent credential would mean unpicking
        that. And an account's prompt cache is what makes a long run
        affordable, so everything sharing a container wants to share an
        account: pinning finer would spread one project's agents across
        accounts and throw the cache away for no gain.

        What the container ends up holding authenticates nothing anywhere.
        """
        if not self.auth_proxy_enabled():
            return
        from ..authproxy import load_secret, mint_token
        vault = self.vault_state(self.AUTH_PROVIDER)[self.AUTH_PROVIDER]
        tag = mint_token(self.slug, load_secret(vault))
        # Only the provider the proxy actually serves. Writing this shape into
        # another provider's profile would be junk at best.
        for host_path, backing in self.private_state(self.AUTH_PROVIDER).items():
            target = backing / ".credentials.json"
            if not backing.is_dir():
                continue
            payload = {"claudeAiOauth": {
                "accessToken": tag,
                # Far enough out that the CLI never tries to renew it. There is
                # nothing to renew with and nothing that needs renewing: the
                # proxy attaches the real token, and this string is only ever
                # a name.
                "expiresAt": int((time.time() + 365 * 86400) * 1000),
                "scopes": ["user:inference"],
                "subscriptionType": "max"}}
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.chmod(0o600)
            os.replace(tmp, target)
            del host_path

    # --- workspace container ----------------------------------------------

    def run_args(self) -> list[str]:
        argv = [
            "docker", "run", "-d", "--init", "--name", self.container,
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
            # Built once. Two --env NO_PROXY flags work — docker takes the
            # last — but "works because of the order they happen to be in" is
            # not a thing to leave in a list somebody will append to.
            no_proxy = ["localhost", "127.0.0.1"]
            if self.auth_proxy_enabled():
                # The hop to the sidecar is inside the network. Sending it out
                # through the egress proxy would be a loop, and the egress
                # allowlist would refuse it anyway.
                no_proxy.append(self.auth_container)
            argv += ["--env", "NO_PROXY=" + ",".join(no_proxy)]

        if self.auth_proxy_enabled():
            from ..authproxy import PORT
            # The model API is reached through something that decides what
            # agents may send with, or it is not reached at all.
            argv += ["--env",
                     f"ANTHROPIC_BASE_URL=http://{self.auth_container}:{PORT}"]

        return argv + [self.image, "sleep", "infinity"]

    def ensure_running(self) -> dict:
        if not docker_available():
            return {"ok": False, "error": "docker is not on PATH"}
        self.seed_private_state()
        self.refresh_private_credentials()
        self.project_placeholder()
        if not self.image_exists(self.image):
            return {"ok": False, "error": f"image {self.image} not built — run `multiagents docker build`"}

        if self.network_mode == "allowlist":
            net = self.ensure_network()
            if not net.get("ok"):
                return {"ok": False, "error": f"network: {net.get('error')}"}
            proxy = self.ensure_proxy()
            if not proxy.get("ok"):
                return {"ok": False, "error": f"proxy: {proxy.get('error')}"}
            auth = self.ensure_auth_proxy()
            if not auth.get("ok"):
                return {"ok": False, "error": f"auth proxy: {auth.get('error')}"}

        state = self.container_state(self.container)
        stale = self.stale_mounts()
        if stale:
            # A mount list is fixed when a container is CREATED. Starting an
            # old one back up gives you the mounts it was born with, so a
            # config change reads as "did nothing" — the toolchain is still
            # missing, the read-only path is still writable, and nothing says
            # why. Refusing is the only way that stops being silent.
            return {"ok": False,
                    "error": f"this container was created without {stale[0]}"
                             + (f" (and {len(stale) - 1} other change(s))"
                                if len(stale) > 1 else "")
                             + ". A mount list is fixed at creation, so "
                               "`docker up` cannot add it: run `multiagents "
                               "docker rm && multiagents docker up`. That ends "
                               "any agent still inside."}
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

    def stale_mounts(self) -> list[str]:
        """Mounts the config asks for that this container does not have.

        Only additions and read-only changes, and only for a container that
        exists — this is about a config edit that cannot take effect, not about
        drift in general.
        """
        if self.container_state(self.container) == "absent":
            return []
        result = _run(["docker", "inspect", "-f",
                       "{{range .Mounts}}{{.Destination}}:{{.RW}}{{\"\\n\"}}{{end}}",
                       self.container])
        if result.returncode != 0:
            return []
        have = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                dest, _, rw = line.rpartition(":")
                have[dest] = rw.strip() == "true"
        missing = []
        for path, read_only in self.mounts():
            # `path` IS the destination. `private_state` maps that destination
            # to the host directory mounted there, which is the SOURCE — look
            # it up here and every container-private mount reads as missing,
            # because the host path is not a destination in the container.
            dest = str(path)
            if dest not in have:
                missing.append(dest)
            elif have[dest] == read_only:
                # Declared read-only and mounted writable, or the reverse.
                missing.append(f"{dest} as {'read-only' if read_only else 'writable'}")
        return missing

    def stop(self, remove: bool = False) -> dict:
        out = {}
        for name in (self.container, self.proxy_container):
            if self.container_state(name) == "absent":
                continue
            out[name] = _run(["docker", "rm", "-f", name] if remove
                             else ["docker", "stop", name]).returncode == 0
        return {"ok": True, "acted_on": out}

    def kill_detached(self, agent_id: str) -> bool:
        """Stop an agent this process did not spawn, from its recorded pid file.

        DockerHandle covers the case where we own the handle; this covers the
        other one — a nested server, or a restart — where all that survives is
        the pid the agent wrote inside the container.
        """
        if self.paths is None:
            return False
        pid_file = self.paths.run_dir(agent_id) / "container.pid"
        try:
            target = pid_file.read_text().strip()
        except OSError:
            return False
        if not target.isdigit():
            return False
        _run(["docker", "exec", self.container, "sh", "-c",
              f"kill -TERM {target} 2>/dev/null; pkill -TERM -P {target} 2>/dev/null; true"],
             timeout=30)
        return True

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

    def _versioned_argv(self, argv: list[str]) -> list[str]:
        """`argv` with a versioned launcher's program named by its resolved
        absolute path, so the container executes the version current at spawn
        time (P0-R1.2) rather than whatever a bind mount pinned at creation.

        Only the program token is touched — arguments that happen to spell a
        provider's bare command name are left alone (P0-R1.2). A launcher that
        is not versioned keeps today's bare-name argv.
        """
        if not argv:
            return argv
        for provider in self.providers.values():
            if provider.bin != argv[0]:
                continue
            binary = provider.available()
            if not binary:
                break
            launcher_path = Path(binary)
            resolved = self._resolve_launcher(binary)
            if self._versions_dir(launcher_path, resolved) is not None:
                return [str(resolved), *argv[1:]]
            break
        return argv

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> Handle:
        state = self.ensure_running()
        if not state.get("ok"):
            raise RuntimeError(f"docker executor: {state.get('error')}")
        argv = self._versioned_argv(argv)

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
            limit=STREAM_LIMIT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return DockerHandle(proc.pid, proc, self.container, pid_file)
