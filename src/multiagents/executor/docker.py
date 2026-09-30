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
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ..paths import ProjectPaths, server_install_paths, state_root
from .. import gitops, procs
from .base import Executor, FollowHandle, Handle, wrapper_argv
from .local import LocalExecutor, _turn_start


class NotRunning:
    """A read-only exec probe found no running container."""


class DockerHandle(Handle):
    """A ``docker exec`` client, plus the ability to stop what it started.

    Killing the client does NOT stop the process inside the container — verified:
    the exec'd command keeps running, keeps spending tokens, and its output goes
    nowhere. So the agent is launched through a shell that records its own pid to
    a file on a bind-mounted path, and stopping means signalling that pid from
    inside the container before killing the local client.

    `stop()` uses `docker exec` unconditionally, and that is fine: this class
    is only ever built by `start()`'s non-inside branch. A server running
    inside the container takes the `_start_inside` branch instead, which hands
    back a plain `Handle` from `LocalExecutor` — no container to exec into,
    and no need for one.
    """

    def __init__(self, pid: int, proc, container: str, pid_file: Path):
        super().__init__(pid=pid, _proc=proc)
        self.container = container
        self.pid_file = pid_file

    def _container_pid(self) -> str | None:
        # SG-R7: the agent's shell wrote it, where the container can write:
        # read through no link, never blocking, and bounded.
        base, parts = gitops.beneath(self.pid_file.parent)
        raw = gitops._read_beneath(base, parts, self.pid_file.name, PID_FILE_MAX_BYTES)
        value = (raw or b"").decode("ascii", errors="replace").strip()
        return value if value.isdigit() else None

    async def stop(self, grace: float = 10.0) -> None:
        target = self._container_pid()
        if target:
            # TERM the agent and everything it started, then KILL what is left.
            wait = int(min(grace, 5.0))
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                await asyncio.to_thread(
                    _run, ["docker", "exec", self.container,
                           *_kill_argv(target, "", wait)], timeout=wait + 30)
        await super().stop(grace=grace)


def _recording_pid(pid_file: Path, argv: list[str]) -> list[str]:
    """`argv`, run through a shell that first writes its own pid to `pid_file`.

    `exec "$@"` replaces the shell, so the recorded pid IS the agent. The path
    goes in as the shell's `$0`, never into the script text: it is built from
    the agent's id, and a quote in that would otherwise be shell syntax.
    """
    return ["sh", "-c", 'echo $$ > "$0"; exec "$@"', str(pid_file), *argv]


# SV-R10: end an agent inside the container. `$0` is the agent's pid (the
# leader of its process group, for a wrapped one), `$1` the wrapper's, either
# possibly empty, `$2` the grace in seconds — as arguments, never as script
# text. The pids are read by the caller: the files are where the agent can
# write. TERM the agent's group, the agent and every process under it, and
# the wrapper, which forwards it; then KILL, the wrapper last, since it is
# what records the exit (SV-R2).
#
# Only what the image has: `sh` (dash, whose builtin `kill` takes no `--`),
# `cat`, `cut`, `sed` and `sleep`; no procps. The tree is read from /proc and
# stopped top down as it is walked, so nothing forks past the walk, and it is
# remembered by pid and start time: once the agent is gone its children are
# someone else's, a child in a session of its own (`setsid`) included, and
# only the walk made before still names them.
_KILL_SCRIPT = r"""
a=$0; w=$1; g=$2
case "$a" in ''|*[!0-9]*) a= ;; esac
case "$w" in ''|*[!0-9]*) w= ;; esac
[ -z "$a$w" ] && exit 3
start() { sed 's/.*) //' /proc/"$1"/stat 2>/dev/null | cut -d' ' -f20; }
live() { s=$(sed 's/.*) //' /proc/"$1"/stat 2>/dev/null | cut -d' ' -f1,20)
  [ -n "$s" ] && [ "${s%% *}" != Z ] && [ "${s%% *}" != X ] \
    && { [ -z "$2" ] || [ "${s#* }" = "$2" ]; }; }
walk() { kill -STOP "$1" 2>/dev/null; echo "$1:$(start "$1")"
  for c in $(cat /proc/"$1"/task/*/children 2>/dev/null); do walk "$c"; done; }
seen=
hit() {
  [ -n "$a" ] && live "$a" && seen="$seen $(walk "$a")"
  for e in $seen; do live "${e%%:*}" "${e#*:}" && kill -$1 "${e%%:*}" 2>/dev/null; done
  [ -n "$a" ] && kill -$1 -"$a" 2>/dev/null
  for e in $seen; do kill -CONT "${e%%:*}" 2>/dev/null; done
  [ -n "$a" ] && kill -CONT -"$a" 2>/dev/null
}
alive() {
  { [ -n "$a" ] && { live "$a" || kill -0 -"$a" 2>/dev/null; }; } && return 0
  for e in $seen; do live "${e%%:*}" "${e#*:}" && return 0; done
  [ -n "$w" ] && live "$w"
}
hit TERM
[ -n "$w" ] && kill -TERM "$w" 2>/dev/null
i=0; while [ $i -lt "$g" ]; do
  alive || exit 0
  sleep 1; i=$((i+1)); done
hit KILL
sleep 1
[ -n "$w" ] && kill -KILL "$w" 2>/dev/null
exit 0
"""


# SV-R1/R6: is the wrapper whose pid `$0` holds still alive, in the pid
# namespace it was recorded in? 0 yes, 1 no (no pid, no process, a zombie).
_ALIVE_SCRIPT = r"""
w=$(cat "$0" 2>/dev/null); case "$w" in ''|*[!0-9]*) exit 1 ;; esac
kill -0 "$w" 2>/dev/null || exit 1
s=$(sed 's/.*) //' "/proc/$w/stat" 2>/dev/null | cut -c1)
[ "$s" = Z ] || [ "$s" = X ] && exit 1
exit 0
"""
# How long the container may go unanswerable before a wrapper nobody can
# see is taken for dead: a daemon restart is seconds, a removed container
# is forever.
UNKNOWN_ALIVE_SECONDS = 60.0


# SV-R1: run the launch wrapper (`$0`, its source) under the container's
# system interpreter, not whichever `python3` the host's PATH names first.
_WRAPPER_ENTRY = ('for p in /usr/bin/python3 /usr/local/bin/python3; do '
                  '[ -x "$p" ] && exec "$p" {flag} "$0" "$@"; done; '
                  'exec python3 {flag} "$0" "$@"')


# SG-R3: git enters the container through `sh`, as every other command here
# does, with its arguments as the shell's, never in the script text. `$0` is
# `git`, `$1` the call's timeout in seconds and `$2` the watchdog's script
# (SG-R7): killing the `docker exec` client does not stop what it started in
# the container, so the timeout is kept in there. The watchdog runs without the call's variable,
# so that it is not among what it ends, and exits 124 when it fired.
_GIT_CALL_KEY = "MULTIAGENTS_GIT_CALL"
_GIT_ENTRY = r"""
t=$1; dog=$2; shift 2
git "$@" & g=$!
env -u MULTIAGENTS_GIT_CALL sh -c "$dog" "$MULTIAGENTS_GIT_CALL" "$t" "$$" & w=$!
wait "$g"; rc=$?
kill "$w" 2>/dev/null; wait "$w"; [ $? -eq 124 ] && exit 124
exit $rc
"""
# `$0` is the call's token, `$1` the timeout, `$2` the entry shell, which is
# spared. Every process carrying the token is stopped with each descendant (one
# that dropped the variable included), top down so none can fork past the
# walk; then all are killed. Repeated, for a process not started at the first
# pass. Read from /proc: the image has no procps.
_GIT_WATCHDOG = r"""
i=0
while [ "$i" -lt "$1" ]; do
  sleep 1; i=$((i+1)); kill -0 "$2" 2>/dev/null || exit 0
done
walk() { kill -STOP "$1" 2>/dev/null; echo "$1"
  for c in $(cat /proc/"$1"/task/*/children 2>/dev/null); do walk "$c"; done; }
n=0
while [ "$n" -lt 3 ]; do
  [ "$n" -gt 0 ] && sleep 1
  roots=$(grep -lzx "MULTIAGENTS_GIT_CALL=$0" /proc/[0-9]*/environ 2>/dev/null \
          | cut -d/ -f3)
  all=$(for r in $roots; do [ "$r" = "$2" ] || walk "$r"; done)
  [ -n "$all" ] && kill -KILL $all 2>/dev/null
  n=$((n+1))
done
exit 124
"""
# A pid file bigger than this is not read (SG-R7).
PID_FILE_MAX_BYTES = 256
# An agent's env file bigger than this is not read (SG-R3): it is under
# `.multiagents`, which the container can write.
ENV_FILE_MAX_BYTES = 1024 * 1024
# A `packed-refs` bigger than this stops `protect_project` (SG-R2): the
# container can write it, and it is read on the host to unpack the base.
PACKED_REFS_MAX_BYTES = 64 * 1024 * 1024


def _mkdirs_nofollow(root: Path, path: Path) -> None:
    """`path` and its missing parents below `root`, as directories. `mkdir`
    never follows a link at the path it creates; one found on the way raises
    rather than being gone through."""
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            os.mkdir(current)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise NotADirectoryError(f"{current} is not a directory") from None


def _create_nofollow(path: Path) -> None:
    """`path` as an empty file if nothing is there; never through a link,
    never opening what is there already (a FIFO would block)."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_NONBLOCK, 0o644))
    except FileExistsError:
        pass


def _turn_start_beneath(base: Path, parts: tuple[str, ...], name: str) -> int:
    """`_turn_start` for a file the container can write (SG-R7): opened
    through no link and never blocking. Whatever is there that is not a
    regular file is removed, and the turn starts a fresh one at 0."""
    try:
        fd = gitops._open_file_beneath(base, parts, name, os.O_RDWR, replace=True)
    except FileNotFoundError:
        return 0
    with os.fdopen(fd, "rb+") as fh:
        end = fh.seek(0, 2)
        if end:
            fh.seek(end - 1)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
                end += 1
        return end


class ContainerGit(gitops.Git):
    """Git for an agent's own commits, run in the project container (SG-R3).

    As the agent itself runs: `docker exec` as its uid, in its worktree, with
    the environment `start` handed it — so the hooks those commits run are as
    sandboxed as the agent that wrote them, filesystem and network alike.

    That environment is read back from the env file `start` wrote. The file
    is under `.multiagents`, which the container can write, so it is read as
    such a file is: only a regular one, through no link at any depth, never
    blocking,
    and bounded. What it says only ever reaches the container, which is where
    whoever could have changed it already is. The environment handed to
    `docker exec` is a fresh file in this host's temporary directory, which
    the container cannot reach.
    """

    def __init__(self, executor: "DockerExecutor", agent_id: str):
        self.executor = executor
        self.agent_id = agent_id

    def environ(self) -> dict[str, str]:
        env: dict[str, str] = {}
        data = self.executor.paths.data
        path = self.executor.env_file(self.agent_id)
        raw = gitops._read_beneath(data, path.parent.relative_to(data).parts,
                                   path.name, ENV_FILE_MAX_BYTES)
        for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key:
                env[key] = value
        # What `start` adds for an agent given no HOME of its own.
        env.setdefault("HOME", str(self.executor.container_home()))
        return env

    def run(self, repo: Path, *args: str, env: dict[str, str] | None = None,
            timeout: int = 120, strip: bool = True) -> gitops.GitResult:
        # Every process of this call carries the token, which is how the
        # watchdog finds them in the container (SG-R7).
        token = os.urandom(12).hex()
        environment = {**self.environ(), **(env or {}), _GIT_CALL_KEY: token}
        fd, env_file = tempfile.mkstemp(prefix="multiagents-git-", suffix=".env")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(f"{k}={v}\n" for k, v in environment.items()
                                 if "\n" not in v))
            proc = subprocess.Popen(
                ["docker", "exec", "--workdir", str(repo),
                 "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", env_file,
                 self.executor.container, "sh", "-c", _GIT_ENTRY, "git",
                 str(max(1, int(timeout))), _GIT_WATCHDOG, "-C", str(repo), *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL,
            )
            try:
                # The watchdog ends it first; this is for a container that
                # does not answer at all.
                stdout, stderr = proc.communicate(timeout=timeout + 30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
        finally:
            os.unlink(env_file)
        if proc.returncode == 124:
            raise subprocess.TimeoutExpired(proc.args, timeout, stdout, stderr)
        out = stdout.strip() if strip else stdout
        return gitops.GitResult(proc.returncode == 0, out, stderr.strip(),
                                proc.returncode)

    @contextlib.contextmanager
    def scratch(self):
        """In the agent's run dir: on the shared bind mount, at the same path
        on both sides. The hook may have replaced what is in it by the time
        it is cleaned up, which must not fail the commit.

        SG-R7: the run dir is reached through no link, and the directory is
        made and removed relative to it, so a link the agent plants on the
        way meanwhile never sends either somewhere else on the host."""
        run_dir = self.executor.paths.run_dir(self.agent_id)
        dfd = gitops._open_beneath(*gitops.beneath(run_dir), create=True)
        try:
            name = f"commit-{os.urandom(6).hex()}"
            os.mkdir(name, 0o700, dir_fd=dfd)
            try:
                yield str(run_dir / name)
            finally:
                with contextlib.suppress(OSError):
                    shutil.rmtree(name, dir_fd=dfd)
        finally:
            os.close(dfd)


def _kill_argv(agent: str, wrapper: str, grace: float) -> list[str]:
    return ["sh", "-c", _KILL_SCRIPT, agent, wrapper, str(max(1, int(grace)))]


def _recorded_pid(run_dir: Path, name: str, data: Path | None = None) -> str:
    """The pid at the head of `name` in `run_dir`, or "" (SG-R7: read through
    no link beneath `data`, never blocking, bounded — the agent can write it)."""
    base, parts = ((data, run_dir.relative_to(data).parts) if data is not None
                   else gitops.beneath(run_dir))
    raw = gitops._read_beneath(base, parts, name, PID_FILE_MAX_BYTES) or b""
    head = raw.decode("ascii", errors="replace").split()[:1]
    return head[0] if head and head[0].isdigit() else ""


PROXY_PORT = 8888

# The variable names `run_args` sets on the container itself (allowlist
# networking's proxy, plus the auth proxy's base URL when it is on) — the one
# place that decides which names carry network/auth-routing configuration
# rather than a credential. `_start_inside` reads the same tuple to give an
# agent spawned *inside* the container the values `run_args` already put in
# its own environment, so the two cannot drift apart into "works from the
# host, breaks from in here" the way SM-R1's live check found.
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
NETWORK_ENV_KEYS = PROXY_ENV_KEYS + ("NO_PROXY", "ANTHROPIC_BASE_URL")

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

    def exec_in_running(self, argv: list[str], timeout: float, *,
                        env: dict[str, str] | None = None) -> tuple[int, str, str] | NotRunning:
        """DM-R6: probe without creating, starting or seeding a container.

        A private process group and pid record allow a second exec to kill
        the container process on timeout. Killing only the Docker client
        leaves its container process alive. Inspection and cleanup share the
        overall timeout + 5 second deadline.
        """
        started = time.monotonic()
        deadline = started + timeout
        cleanup_deadline = deadline + 5

        def remaining(until: float) -> float:
            return max(0.001, until - time.monotonic())

        try:
            status = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", self.container],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=remaining(deadline))
        except FileNotFoundError:
            return NotRunning()
        except subprocess.TimeoutExpired:
            return 124, "", "container inspection timed out"
        if status.returncode or status.stdout.strip() != "running":
            return NotRunning()

        pidfile = f"/tmp/multiagents-probe-{uuid.uuid4().hex}.pid"
        wrapper = """import os, subprocess, sys
try:
    os.setsid()
except PermissionError:
    pass
path = sys.argv[1]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(str(os.getpid()))
try:
    try:
        rc = subprocess.call(sys.argv[2:], stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        rc = 127
finally:
    os.unlink(path)
sys.exit(rc)
"""
        cleanup = "import os, signal, sys; p = sys.argv[1]; text = open(p).read().strip() if os.path.exists(p) else ''; pid = int(text) if text.isdigit() else 0; os.killpg(pid, signal.SIGKILL) if pid else None; os.unlink(p) if os.path.exists(p) else None"
        command = ["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}"]
        # Env files keep credentials out of the host process list. The
        # running container retains its configured proxy environment.
        with tempfile.NamedTemporaryFile(mode="w", prefix="multiagents-probe-") as envfile:
            if env is not None:
                envfile.write("".join(f"{k}={v}\n" for k, v in env.items()
                                      if "\n" not in str(v)))
                envfile.flush()
                command += ["--env-file", envfile.name]
            command += [self.container, "/usr/bin/python3", "-c", wrapper, pidfile, *argv]
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
            try:
                out, err = child.communicate(timeout=remaining(deadline))
                return child.returncode, out, err
            except subprocess.TimeoutExpired:
                killer = subprocess.Popen(
                    ["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}",
                     self.container, "/usr/bin/python3", "-c", cleanup, pidfile],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                try:
                    killer.wait(timeout=min(3, remaining(cleanup_deadline)))
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(killer.pid, signal.SIGKILL)
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    for proc in (killer, child):
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            proc.wait(timeout=remaining(cleanup_deadline))
                    child.stdout.close()
                    child.stderr.close()
                return 124, "", f"probe timed out after {timeout}s"
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=1)
                child.stdout.close()
                child.stderr.close()
                raise

    # ------------------------------------------------------------- naming --

    @property
    def slug(self) -> str:
        return self.paths.slug if self.paths else "default"

    @property
    def container(self) -> str:
        return self.config.get("container_name") or f"multiagents-{self.slug}"

    def inside(self) -> bool:
        """Is this process already running in this project's container?

        True for the MCP server of an agent with spawn rights (SM-R1): its CLI
        starts it in here, where there is no `docker` and no socket to reach
        the daemon with. Its own spawns are therefore started directly — they
        are in the container already, which is the whole of what `docker
        exec` would have given them.

        Never decided by this process's own environment, which anything on
        the host can set: a wrong True runs agents on the host, with no egress
        proxy and no resource limits. The container's init process carries
        its name from creation (`run_args`), and docker marks every container
        with `/.dockerenv`; a host process can fake neither. A container
        created before the name was given reads as outside, and its spawns
        then go to `docker exec`, which is not in here and fails — closed.
        """
        if not Path("/.dockerenv").exists():
            return False
        try:
            init_env = Path("/proc/1/environ").read_bytes().split(b"\0")
        except OSError:
            return False
        return f"MULTIAGENTS_CONTAINER={self.container}".encode() in init_env

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
        target is a FILE in a directory other than the launcher's own, AND
        whose file name differs from the launcher's own name — that
        directory counts as the versions directory whatever its name, even
        holding a single entry. A target nested below a per-version directory
        with the launcher's own name (``versions/1.0.0/bin/x`` for launcher
        ``bin/x``) is out of scope, since the version there lives in the
        directory, not the file name: it reads as unversioned here and keeps
        today's behaviour (P0-R1.8).
        """
        if resolved == launcher or not resolved.is_file():
            return None
        if resolved.parent == launcher.parent:
            return None
        if resolved.name == launcher.name:
            return None
        return resolved.parent

    # CX-C17: what a versions root may never be, nor contain. `/lib*` is
    # matched by name below.
    SYSTEM_PREFIXES = (Path("/usr"), Path("/bin"), Path("/sbin"), Path("/etc"),
                       Path("/opt"), Path("/var"), Path("/nix"), Path("/snap"))

    @staticmethod
    def _host_launcher(provider: Any) -> str | None:
        """The host CLI's path as found, before symlinks are resolved (PS-R2).
        A provider without `resolve_bin` answers through `available()`."""
        resolve = getattr(provider, "resolve_bin", None)
        if resolve is None:
            return getattr(provider, "available", lambda: None)()
        # PS-R1a: the mount derivation resolves against the same environment a
        # native launch of this provider would — the ambient PATH with the
        # instance's own `env:` applied (an instance may point PATH at the
        # directory holding its account's CLI) — never the server's PATH alone.
        merged = dict(os.environ)
        for key, value in (getattr(provider, "env", None) or {}).items():
            merged[key] = os.path.expanduser(os.path.expandvars(str(value)))
        launcher = resolve(env=merged).launcher
        return str(launcher) if launcher is not None else None

    @staticmethod
    def _declares_depth(provider: Any) -> bool:
        """Whether `provider` asks for a versions root that is to be honoured:
        `bin_versions_depth` set on an enabled provider (CX-C3, CX-C20)."""
        return ((getattr(provider, "bin_versions_depth", 0) or 0) >= 1
                and getattr(provider, "enabled", True))

    @classmethod
    def _depth_error(cls, provider: Any, resolved: Path) -> str:
        """Why the versions root `bin_versions_depth` computes for `resolved`
        must not be mounted, or "" (CX-C17): deeper than the path allows, or a
        root that would widen the mount to the home, `/` or a system prefix.
        """
        depth = provider.bin_versions_depth
        key = f"{provider.name}'s bin_versions_depth: {depth}"
        if len(resolved.parents) < depth:
            return (f"{key} is deeper than {resolved}, which has only "
                    f"{len(resolved.parents)} directories above it")
        root = resolved.parents[depth - 1]
        homes = {Path.home(), Path.home().resolve()}
        if (root == Path("/") or root in homes
                or any(root in home.parents for home in homes)):
            reason = "the home directory or one above it"
        elif (root in cls.SYSTEM_PREFIXES
              or (len(root.parts) == 2 and root.parts[1].startswith("lib"))):
            reason = "a system prefix"
        else:
            return ""
        return (f"{key} makes {root} the versions root of {resolved}, and that "
                f"is {reason}: mounting it would expose far more than the CLI. "
                f"Lower the depth to the directory that holds every version")

    @classmethod
    def _depth_root(cls, provider: Any, resolved: Path) -> Path | None:
        """The versions root `bin_versions_depth` declares (CX-C3), or None.

        `resolved.parents[N-1]`: N = 1 is the target's own directory. None
        when the key is unset or not honoured (`_declares_depth`), and when
        the root is refused (`_depth_error`): a refused root is never
        mounted, and the run is refused in `ensure_running`.
        """
        if not cls._declares_depth(provider) or not resolved.is_file():
            return None
        if cls._depth_error(provider, resolved):
            return None
        return resolved.parents[provider.bin_versions_depth - 1]

    def _depth_errors(self) -> list[str]:
        """Every refused versions root among the installed providers (CX-C17).
        None with `mount_cli_from_host: false`, which mounts no root (CX-C18)."""
        if not self.config.get("mount_cli_from_host", True):
            return []
        out = []
        for provider in self.providers.values():
            if not self._declares_depth(provider):
                continue
            binary = self._host_launcher(provider)
            if not binary:
                continue
            resolved = self._resolve_launcher(binary)
            if resolved.is_file():
                error = self._depth_error(provider, resolved)
                if error:
                    out.append(error)
        return out

    def _versions_roots(self) -> dict[Path, str]:
        """{versions root: provider name} for every enabled provider declaring
        `bin_versions_depth`, as its launcher resolves right now. Empty with
        `mount_cli_from_host: false` (CX-C18)."""
        out: dict[Path, str] = {}
        if not self.config.get("mount_cli_from_host", True):
            return out
        for name, provider in self.providers.items():
            if not self._declares_depth(provider):
                continue
            binary = self._host_launcher(provider)
            if not binary:
                continue
            root = self._depth_root(provider, self._resolve_launcher(binary))
            if root is not None:
                out.setdefault(root, name)
        return out

    def _resolve_launcher(self, binary: str) -> Path:
        """Where `binary`'s host launcher points RIGHT NOW.

        Docker fixes a bind mount's target at container creation; a versioned
        launcher's target moves after that. Resolving on the host at every
        spawn — rather than trusting whatever `mounts()` last computed — is
        what makes the container run the CLI version current at spawn time
        without being recreated (P0-R1.2).
        """
        return Path(binary).resolve()

    @staticmethod
    def server_mounts(base: list[tuple[Path, bool]]) -> list[Path]:
        """What the multiagents server needs mounted that `base` does not reach.

        SM-R1: an agent with spawn rights starts the server in here, by the
        interpreter running multiagents on the host (`paths.server_command`).
        A checkout of multiagents used as its own project already has all of
        it under the root, and then nothing is added.

        Best effort, and deliberately never a reason to call a container
        stale (`stale_mounts`, `mount_drift`): the interpreter differs between
        entry points — an installed `multiagents` and a checkout's venv — so
        requiring it would make whichever ran second refuse every spawn. A
        container without it runs its agents without the server, and the run
        records that (SM-R5).
        """
        covered = [path for path, _ in base]
        out: list[Path] = []
        for path in sorted(server_install_paths(), key=lambda p: len(p.parts)):
            if not any(path == c or c in path.parents for c in covered):
                out.append(path)
                covered.append(path)
        return out

    def mounts(self) -> list[tuple[Path, bool]]:
        """(host path, read_only) pairs, each mounted at its own path.

        Three groups: the project and its git worktrees (required at identical
        paths for git to resolve; the worktrees writable, the project root split
        by `project_mounts`); the CLI binaries (read-only);
        and each provider's credential/state directory, taken from the
        ``home_links`` already declared in providers.yaml so this list cannot
        drift from what the per-agent HOME expects to find.
        """
        if self.paths is None:
            return []
        # Listed whether or not they exist yet: `protect_project` creates the
        # missing ones before a container is created, and a protection that
        # vanished from the list because its path was absent would be a
        # protection nobody asked to drop (SG-R2).
        project = self.project_mounts()
        out: list[tuple[Path, bool]] = [
            *project,
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
                binary = self._host_launcher(provider)
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
                    if (self._declares_depth(provider) and resolved.is_file()
                            and (self._depth_root(provider, resolved) is not None
                                 or self._depth_error(provider, resolved))):
                        # Its versions root is mounted last, below; a refused
                        # one is not mounted at all, nor anything under it
                        # (CX-C17).
                        pass
                    elif versions_dir is not None:
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

        # CX-C1: an adapter runs at its host path, so it must be visible
        # there — read-only, as every other executable mounted in is.
        for adapter in self._adapter_paths():
            covering = [(p, ro) for p, ro in out if p == adapter or p in adapter.parents]
            deepest = max(covering, key=lambda pair: len(pair[0].parts), default=None)
            if deepest is None or not deepest[1]:
                out.append((adapter, True))

        required = {path for path, _ in project}
        seen: dict[Path, bool] = {}
        for path, read_only in out:
            if (path.exists() or path in required) and path not in seen:
                seen[path] = read_only
        mounts = sorted(seen.items())

        # Container-private state is mounted OVER the host path, so a per-agent
        # HOME's symlinks still resolve while the host's own credentials stay
        # untouched and unreachable.
        for host_path, private_path in self.private_state().items():
            private_path.mkdir(parents=True, exist_ok=True)
            mounts.append((host_path, False))
        # And a provider's session transcripts, where no private home already
        # holds them (SP-R1): in the container layer they die with it.
        for container_path, store in self.transcript_state().items():
            store.mkdir(parents=True, exist_ok=True)
            mounts.append((container_path, False))
        # CX-C3: a versions root, read-only and nothing above it, so a self-
        # update under it reaches the container without a recreate. Last,
        # because it may nest inside a private home mounted just above: the
        # deeper mount has to come after the one it sits in.
        listed = {path for path, _ in mounts}
        for root in self._versions_roots():
            if root not in listed:
                mounts.append((root, True))
                listed.add(root)
        from ..authority import HostAuthority
        authority = HostAuthority.directory_for(self.paths).resolve()
        for source, _ in mounts:
            mounted = source.resolve()
            if (mounted == authority or mounted in authority.parents
                    or authority in mounted.parents):
                raise ValueError(f"mount {source} would expose host authority records")
        return mounts

    def _adapter_paths(self) -> list[Path]:
        """The host path of every enabled provider's `adapter:` that is
        installed (CX-C20)."""
        from .. import scripts
        from ..paths import global_config_dir

        config_dir = self.config_dir or global_config_dir()
        project_config = self.paths.config if self.paths is not None else None
        out = []
        for provider in self.providers.values():
            if not getattr(provider, "enabled", True):
                continue
            found = scripts.resolve_adapter(provider, config_dir, project_config)
            if found is not None and found not in out:
                out.append(found)
        return out

    # Under `.git`, read-only from the container (SG-R2). Each one is
    # something host-side git trusts: what it executes (hooks, config,
    # submodule config), what it takes the main checkout to be (HEAD, index,
    # info/exclude), and which commits the user's branches and tags name.
    # `objects/info` holds `alternates`, which every git process follows: an
    # entry the container wrote could hang a host read or feed it foreign
    # objects. None is a mount of its own: they are protected because `.git`
    # is read-only, or, for the directories, by the mount that closes them
    # again inside a writable one. They are listed because `protect_project`
    # checks them and makes them exist.
    GIT_PROTECTED = ("config", "config.worktree", "hooks", "info", "modules",
                     "HEAD", "index", "refs/heads", "refs/tags", "objects/info")
    # The protected paths that are directories; the rest are files.
    GIT_PROTECTED_DIRS = ("hooks", "info", "modules", "refs/heads", "refs/tags",
                          "objects/info")
    # The directories reopened writable beneath the read-only `.git`, and the
    # ones closed again inside them. Directories only: a single-file bind
    # mount inside a directory the host writes vanishes the moment the host
    # replaces that file by rename, as every `git config` and index refresh
    # does, and the protection goes with it.
    GIT_WRITABLE_DIRS = ("objects", "refs", "logs", "worktrees")
    GIT_CLOSED_DIRS = ("objects/info", "refs/heads", "refs/tags")
    # Inside `refs/heads`, the namespace agent branches are created in.
    AGENT_REFS = "refs/heads/agents"

    def project_mounts(self) -> list[tuple[Path, bool]]:
        """The project root's mounts: what the container may change in it and
        what it may not (SG-R2).

        The project root was once mounted writable whole, `.git` included, so
        an agent could write a hook, `core.fsmonitor` or a filter driver that
        host-side git then ran outside the container, or edit the main
        checkout and `project.yaml`, which is this container's own boundary.

        So the root is read-only, and `.git` with it. What the container
        genuinely writes is reopened below it, each at its own path, where the
        deepest mount wins, and only ever as a directory: `objects`, `refs`,
        `logs` and `worktrees` in `.git`, the agent-branch namespace, and
        `.multiagents` runtime state (run dirs, the event stream, tree.json).
        `objects/info`, `refs/heads` and `refs/tags` are closed again inside
        those. There is no single-file mount: one inside a writable directory
        disappears when the host renames a new file over it (found live, on
        `.git/config` and `.git/index`). The cost is that the container cannot
        rewrite `packed-refs`, nor take `packed-refs.lock`, which every ref
        deletion does.

        A project whose `.git` is a file is not covered: `ensure_running`
        refuses it, and nothing under it is listed here.
        """
        root = self.paths.root
        git = root / ".git"
        out: list[tuple[Path, bool]] = [(root, True)]
        if git.is_dir():
            out.append((git, True))
            out += [(git / rel, False) for rel in self.GIT_WRITABLE_DIRS]
            out += [(git / rel, True) for rel in self.GIT_CLOSED_DIRS]
            out.append((git / self.AGENT_REFS, False))
        out += [(self.paths.data, False), (self.paths.config, True)]
        return out

    def protect_project(self) -> str:
        """Make every path `project_mounts` protects exist, and unpack the base
        branch; the error that stops a container being used, or "".

        A bind mount's source must exist, and one docker creates for itself is
        a root-owned directory, whatever the path was meant to be. So missing
        mounted directories are created empty, and so are the other protected
        ones (`hooks`, `info`, `modules`), so that a container which could once
        write `.git` has not left something of its own there. `config.worktree`
        is created empty, which git reads as a config with nothing in it. A
        missing index is written by git itself: a zero-byte one makes every
        host git command fail, and an empty index is exactly what a missing
        one means.

        The base branch, the one checked out in the main checkout, gets a
        loose ref file, since a loose ref wins over a packed one. `packed-refs`
        is read-only from the container now that `.git` is, but it was not
        always, and the loose ref is what the base branch's protection rests
        on inside the `refs/heads` directory mount. Run on every call rather
        than only at creation, because the user may check out another branch
        while the container runs; a ref file written in `refs/heads` later is
        protected as well.
        """
        if self.paths is None:
            return ""
        root = self.paths.root
        git = root / ".git"
        if git.is_file():
            return (f"{git} is a file, not a directory: this project is itself a "
                    "linked worktree or uses a separate git dir, and the docker "
                    "executor cannot protect its repository from the container. "
                    "Run it from a main checkout, or use the local executor.")
        try:
            if git.is_dir():
                dirs = (*self.GIT_WRITABLE_DIRS, *self.GIT_PROTECTED_DIRS,
                        self.AGENT_REFS)
                files = [rel for rel in self.GIT_PROTECTED if rel not in dirs]
                for rel in dirs:
                    error = self._plain(root, git / rel, directory=True)
                    if error:
                        return error
                for rel in files:
                    error = self._plain(root, git / rel, directory=False)
                    if error:
                        return error
                for rel in dirs:
                    _mkdirs_nofollow(root, git / rel)
                for rel in ("config", "config.worktree"):
                    _create_nofollow(git / rel)
                if not os.path.lexists(git / "index"):
                    error = self._create_index(git)
                    if error:
                        return error
                error = self._unpack_base(root, git)
                if error:
                    return error
            error = self._plain(root, self.paths.config, directory=True)
            if error:
                return error
            _mkdirs_nofollow(root, self.paths.config)
        except OSError as e:
            return f"cannot prepare {root} for the container: {e}"
        return ""

    @staticmethod
    def _plain(root: Path, path: Path, directory: bool) -> str:
        """Why `path` cannot be protected, or "" when it is safe to create or
        use: every component below `root` is absent or a real directory, and
        `path` itself is absent or a real directory (`directory`) or regular
        file. A symlink or a FIFO there was planted while the container could
        write it, and `protect_project` must neither write through it nor
        block on it. Checked with `lstat` only, which never follows nor opens.
        """
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                return ""
            if stat.S_ISLNK(mode):
                return (f"{current} is a symlink; refusing to protect {path} "
                        "through it. Remove it and try again.")
            last = current == path
            if stat.S_ISDIR(mode) and (not last or directory):
                continue
            if last and not directory and stat.S_ISREG(mode):
                continue
            return (f"{current} is not a plain {'directory' if directory or not last else 'file'}; "
                    f"refusing to protect {path}. Remove it and try again.")
        return ""

    @staticmethod
    def _create_index(git: Path) -> str:
        """An empty index, written by git, with nothing the repository
        configures run on the way: no hook (`post-index-change` fires on any
        index write) and no fsmonitor."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(
            ["git", f"--git-dir={git}", "-c", "core.hooksPath=/dev/null",
             "-c", "core.fsmonitor=false", "read-tree", "--empty"],
            cwd=git.parent, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            return f"cannot create {git / 'index'}: {result.stderr.strip()[:300]}"
        return ""

    @classmethod
    def _unpack_base(cls, root: Path, git: Path) -> str:
        """Give the checked-out branch a loose ref if it only has a packed one.

        Read from the files rather than asked of git: this runs on the host,
        in a repository the container could write to until now. Nothing moves:
        the loose file holds the sha `packed-refs` already names, written by
        lock-and-rename as git itself does. A detached HEAD has no branch to
        unpack, and an unborn branch has no sha: both are left alone. The
        files are read without following a link and without blocking, since
        the container can still write `packed-refs`.
        """
        raw = gitops._read_regular(git / "HEAD", 4096)
        if raw is None and os.path.lexists(git / "HEAD"):
            return f"{git / 'HEAD'} is not a readable regular file"
        head = (raw or b"").decode(errors="replace").strip()
        if not head.startswith("ref: refs/heads/"):
            return ""
        name = head[len("ref: "):]
        loose = git / name
        heads = git / "refs" / "heads"
        if heads not in loose.parents or ".." in Path(name).parts:
            return f"{git / 'HEAD'} names an unexpected ref: {name}"
        error = cls._plain(root, loose, directory=False)
        if error:
            return error
        if os.path.lexists(loose):
            return ""
        packed = git / "packed-refs"
        sha = ""
        if os.path.lexists(packed):
            data = gitops._read_regular(packed, PACKED_REFS_MAX_BYTES)
            if data is None:
                return (f"{packed} is not a regular file of at most "
                        f"{PACKED_REFS_MAX_BYTES} bytes; cannot unpack {name}")
            for line in data.decode(errors="replace").splitlines():
                if line[:1] in ("#", "^"):
                    continue
                value, _, ref = line.partition(" ")
                if ref.strip() == name:
                    sha = value.strip()
                    break
        if not sha:
            return ""
        _mkdirs_nofollow(root, loose.parent)
        lock = loose.with_name(loose.name + ".lock")
        try:
            # O_EXCL: never through a link someone left at the lock's path.
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            return (f"cannot unpack the base branch {name}: {lock} exists, so "
                    "another git process holds it. Try again once it is done.")
        with os.fdopen(fd, "w") as f:
            f.write(sha + "\n")
        os.replace(lock, loose)
        return ""

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

        if self.inside():
            return []                  # the host's job; nothing in here can do it
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
        private = self.backing()
        # The server's own install paths differ by entry point, so they are
        # neither owed nor surplus: left out on both sides. Only those
        # `run_args` adds, though — one the configuration mounts itself is
        # owed like any other, and dropping it from `have` alone would report
        # it missing for ever.
        optional = {str(p) for p in self.server_mounts(self.mounts())}
        have = {line.strip() for line in result.stdout.splitlines()
                if line.strip() and line.strip().split(">")[-1] not in optional}
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

    def transcript_state(self, provider: str = "") -> dict[Path, Path]:
        """{static transcript prefix in the container: its store on the host}.

        SP-R1. Every provider that declares where it writes its sessions gets
        that place backed by the host, or a `docker rm` deletes every
        conversation an agent could have been resumed from — which is how
        they were all lost on 2026-09-24. Generic: the only input is the
        provider's own `transcript:` declaration.

        Only where nothing else holds it. A prefix inside a container-private
        home (a `~/.<cli>/projects` inside a private `~/.<cli>`) is host-backed
        already, and a second mount over it would split one profile across two
        stores. The transcript directory alone, never the profile around it:
        credentials must not become reachable through this.

        Per project, whatever `credential_scope` says: sessions are work, not
        an account.
        """
        if self.paths is None:
            return {}
        from ..watchdog import transcript_prefix

        held = list(self.private_state())
        root = state_root() / "transcripts" / self.slug
        home = self.container_home()
        out: dict[Path, Path] = {}
        for name, entry in self.providers.items():
            if provider and name != provider:
                continue
            prefix = transcript_prefix(entry, home)
            if prefix is None or not prefix.is_absolute():
                continue
            if any(prefix == p or p in prefix.parents for p in held):
                continue
            relative = (prefix.relative_to(home) if home in prefix.parents
                        else prefix.relative_to(prefix.anchor))
            out[prefix] = root / name / relative
        return out

    def backing(self) -> dict[Path, Path]:
        """{path in the container: the host directory mounted there}, for every
        mount whose source is not the path itself."""
        return {**self.private_state(), **self.transcript_state()}

    def container_home(self) -> Path:
        """HOME inside the container: the host's, by construction.

        Every host path is mounted at its own location, so a per-agent HOME's
        links to `~/<x>` resolve to the same `Path.home() / x` in here as on
        the host. And an agent given no HOME (home_policy shared or host) is
        handed the host's at exec time (`start`) — the uid it runs as has no
        passwd entry in the image, so it would otherwise get `/`.
        """
        return Path.home()

    def host_path(self, path: Path) -> Path:
        """Where `path`, as the container sees it, lives on the host (SP-R2).

        Followed through the deepest mount that relocates it; every other
        mount is at its own path, so anything else is where it says. From
        inside the container the container's view is the one to read.

        `..` is normalised first, as the container would: compared as written,
        `~/.profile/../x` sits under `~/.profile` and maps to a host path that
        climbs out of its store.
        """
        if self.paths is None or self.inside():
            return path
        path = Path(os.path.normpath(path))
        best = None
        for destination, source in self.backing().items():
            if path == destination or destination in path.parents:
                if best is None or len(destination.parts) > len(best[0].parts):
                    best = (destination, source)
        if best is None:
            return path
        return best[1] / path.relative_to(best[0])

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

    def oom_kill_count(self) -> int | None:
        """LN-C5: the container cgroup's `oom_kill` counter, or None when it
        cannot be read. Container-wide: every OOM decision goes through here,
        and attributing a rise to one run is the caller's job.

        Inside the container the cgroup namespace root is its own cgroup.
        From the host it is found by the container's full id under the
        layouts docker uses: systemd with cgroup v2 (the minimum supported),
        cgroupfs v2, and both under cgroup v1.
        """
        if self.inside():
            candidates = [Path("/sys/fs/cgroup/memory.events"),
                          Path("/sys/fs/cgroup/memory/memory.oom_control")]
        else:
            try:
                result = _run(["docker", "inspect", "-f", "{{.Id}}", self.container],
                              timeout=10)
            except (OSError, subprocess.SubprocessError):
                return None
            cid = result.stdout.strip()
            if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", cid):
                return None
            root = Path("/sys/fs/cgroup")
            candidates = [root / "system.slice" / f"docker-{cid}.scope" / "memory.events",
                          root / "docker" / cid / "memory.events",
                          root / "memory" / "system.slice" / f"docker-{cid}.scope"
                          / "memory.oom_control",
                          root / "memory" / "docker" / cid / "memory.oom_control"]
        for path in candidates:
            try:
                text = path.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                name, _, value = line.partition(" ")
                if name == "oom_kill" and value.strip().isdigit():
                    return int(value.strip())
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
            # What `inside` recognises the container by, from within it.
            "--env", f"MULTIAGENTS_CONTAINER={self.container}",
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

        private = self.backing()
        mounts = self.mounts()
        for path, read_only in mounts:
            source = private.get(path, path)
            argv += ["-v", f"{source}:{path}" + (":ro" if read_only else "")]
        # Added here and not in `mounts()`, which lists what a container is
        # owed: these are best effort (see `server_mounts`).
        for path in self.server_mounts(mounts):
            argv += ["-v", f"{path}:{path}:ro"]

        if self.network_mode == "allowlist":
            proxy = f"http://{self.proxy_container}:{PROXY_PORT}"
            for name in PROXY_ENV_KEYS:
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
        if self.inside():
            # Called from here, we ARE the container: it is by definition
            # running, on the image it was built from, with no `docker` to
            # ask. `start()` never reaches this for an inside spawn (it takes
            # the `_start_inside` branch first) — this guard is for any other
            # caller that assumes `ensure_running` is always safe to call.
            return {"ok": True, "container": self.container, "existed": True}
        # CX-C17: a versions root that would widen the mount is a config
        # error, refused before any container is created or used.
        errors = self._depth_errors()
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        # Before anything is created or started: a container must never run
        # with a protected path missing, and a project it cannot protect is
        # refused outright (SG-R2).
        error = self.protect_project()
        if error:
            return {"ok": False, "error": error}
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
            roots = {str(root): name for root, name in self._versions_roots().items()}
            root = next((dest for dest in stale if dest in roots), None)
            if root is not None:
                # CX-C3/C19/C27: `bin` resolves under a versions root this
                # container was not created with — an update that moved the
                # install, or the key added since. One more drift item, said
                # by name so the fix is findable, and led with because it is
                # the one that stops a provider running at all.
                name = roots[root]
                first = (f"this container lacks the versions root {root} "
                         f"({name}'s bin_versions_depth: "
                         f"{self.providers[name].bin_versions_depth}), so it "
                         f"cannot run {name}'s `bin`")
            else:
                first = f"this container was created without {stale[0]}"
            warning = self.recreation_warning()
            return {"ok": False,
                    "error": first
                             + (f" (and {len(stale) - 1} other change(s))"
                                if len(stale) > 1 else "")
                             + ". A mount list is fixed at creation, so "
                               "`docker up` cannot add it"
                             + (f". {warning}" if warning else
                                ": run `multiagents docker rm && multiagents "
                                "docker up`. That ends any agent still inside.")}
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

    # What a container recreation ends: an agent whose process is in it.
    ENDED_BY_RECREATION = ("running", "detached", "stuck")

    def agents_inside(self) -> list[tuple[str, str]]:
        """`(id, status)` of every agent `docker rm` would end (SP-R5).

        Every live node of the project: a node records no executor of its own,
        so under this one they are taken to be in here. The launched sessions
        (drivers) run on the host and are not.
        """
        if self.paths is None or not self.paths.tree_file.is_file():
            return []
        from ..tree import DRIVER_ROLES, Tree
        try:
            nodes = Tree(self.paths.tree_file, self.paths.events_file).read()["nodes"]
        except Exception:
            return []
        return sorted((agent_id, raw.get("status", "")) for agent_id, raw in nodes.items()
                      if raw.get("status") in self.ENDED_BY_RECREATION
                      and raw.get("role", "") not in DRIVER_ROLES)

    def recreation_warning(self) -> str:
        """The agents a recreation would end, and what to do first; "" for
        none. A prescription of `docker rm` that `docker rm` itself then
        refuses is no prescription at all (SP-R5)."""
        inside = self.agents_inside()
        if not inside:
            return ""
        listed = ", ".join(f"{agent_id} ({status})" for agent_id, status in inside)
        return (f"Recreating it ends the agents still inside: {listed}. Run "
                f"`multiagents stop` first, then `multiagents docker rm && "
                f"multiagents docker up`.")

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

    def kill_detached(self, agent_id: str, grace: float = 3.0) -> bool:
        """Stop an agent this process did not spawn, from its recorded pid file.

        DockerHandle covers the case where we own the handle; this covers the
        other one — a nested server, or a restart — where all that survives is
        the pid the agent wrote inside the container.

        A server running inside the container reaches its own children's pids
        directly (SM-R1): `pid_file` was written by `_recording_pid` in
        *this* process's pid namespace, so a plain `kill`/`pkill` here IS the
        equivalent of `docker exec`, with no daemon to ask.

        SV-R10: escalates to KILL after `grace` seconds, since an agent that
        ignores TERM must still end; blocks for at most `grace` + 1 seconds.
        """
        if self.paths is None:
            return False
        run_dir = self.paths.run_dir(agent_id)
        agent = _recorded_pid(run_dir, "container.pid", self.paths.data)
        wrapper = _recorded_pid(run_dir, "wrapper.pid", self.paths.data)
        if not (agent or wrapper):
            return False
        argv = _kill_argv(agent, wrapper, grace)
        if not self.inside():
            argv = ["docker", "exec", self.container, *argv]
        try:
            return _run(argv, timeout=int(grace) + 30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def wrapper_alive(self, agent_id: str) -> bool | None:
        """Whether an agent's wrapper is alive in the container, asked from
        there — the host cannot tell: the `docker exec` client it holds can
        die while the wrapper carries on. None when the container cannot be
        asked. Blocking."""
        if self.paths is None:
            return None
        argv = ["sh", "-c", _ALIVE_SCRIPT,
                str(self.paths.run_dir(agent_id) / "wrapper.pid")]
        if not self.inside():
            argv = ["docker", "exec", self.container, *argv]
        try:
            code = _run(argv, timeout=30).returncode
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {0: True, 1: False}.get(code)

    def liveness(self, agent_id: str):
        """A `FollowHandle.probe` for a wrapped agent in the container. An
        unanswerable container counts as alive for `UNKNOWN_ALIVE_SECONDS`,
        then as dead: long enough to ride out a daemon restart, short enough
        that a removed container does not leave a run followed forever."""
        state = {"unknown_since": None}

        def probe() -> bool:
            answer = self.wrapper_alive(agent_id)
            if answer is not None:
                state["unknown_since"] = None
                return answer
            if state["unknown_since"] is None:
                state["unknown_since"] = time.monotonic()
            return time.monotonic() - state["unknown_since"] < UNKNOWN_ALIVE_SECONDS

        return probe

    # -------------------------------------------------------------- execute --

    def preflight(self) -> list[str]:
        if self.inside():
            # Every check below asks whether the container exists, is built
            # from the right image, and is safe to start — all moot from in
            # here: it exists (we are it), its image is what it was started
            # from, and `mount_docker_socket` is a host-launch decision this
            # process cannot itself have made. Nothing to check, no `docker`
            # to check it with.
            return []
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
        if not argv or not self.config.get("mount_cli_from_host", True):
            # CX-C18: with no host CLI mounted, the bare name is the one the
            # container's own PATH resolves.
            return argv
        for provider in self.providers.values():
            if provider.bin != argv[0]:
                continue
            found = provider.resolve_bin()
            binary = found.launcher
            if not binary:
                break
            launcher_path = Path(binary)
            resolved = self._resolve_launcher(binary)
            if (self._depth_root(provider, resolved) is not None
                    or self._versions_dir(launcher_path, resolved) is not None):
                return [str(resolved), *argv[1:]]
            if found.via == "bin_search":
                return [str(launcher_path), *argv[1:]]
            break
        return argv

    def native_bin(self, provider_name: str, provider: Any, env: dict[str, str]) -> str:
        """CX-C2/C3: `bin` as the container can run it, resolved on the host
        at this exec. A versions root (`bin_versions_depth`) or a P0-R1
        versions directory is mounted whole, so the target current NOW is
        reachable; otherwise the launcher, which is mounted at its own path.

        From inside the container (a spawn at depth >= 2) the host launcher
        cannot be seen: this is the launcher as mounted, the version current
        when the container was created. It runs; it may be stale.

        With `mount_cli_from_host: false` the host's CLI is not in the
        container: the bare `bin`, for the container's PATH (CX-C18).
        """
        if not self.config.get("mount_cli_from_host", True):
            return provider.bin
        binary = provider.resolve_bin(env={**os.environ, **env}).launcher
        if not binary:
            return ""
        launcher = Path(binary)
        resolved = self._resolve_launcher(binary)
        if (self._depth_root(provider, resolved) is not None
                or self._versions_dir(launcher, resolved) is not None):
            return str(resolved)
        return str(binary)

    def adapter_env(self, argv: list[str], env: dict[str, str],
                    provider: str = "") -> dict[str, str]:
        """The base variables, plus MULTIAGENTS_PRIVATE_HOME when this
        provider has a private home in the container, as actions get it."""
        found = self.adapter_provider(argv, provider)
        env = super().adapter_env(argv, env, provider)
        if found is not None:
            private = self.private_state(found[0])
            if private:
                env["MULTIAGENTS_PRIVATE_HOME"] = str(next(iter(private)))
        return env

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str], *,
                    run_dir: Path | None = None, deadline: float = 0,
                    provider: str = "") -> Handle:
        if self.inside():
            return await self._start_inside(argv, cwd, env, run_dir=run_dir,
                                            deadline=deadline, provider=provider)
        state = self.ensure_running()
        if not state.get("ok"):
            raise RuntimeError(f"docker executor: {state.get('error')}")
        argv = self._versioned_argv(argv)
        env = self.adapter_env(argv, env, provider)
        if "HOME" not in env:
            # What `container_home` promises. Locally the CLI falls back to the
            # passwd entry, which is this; in the image there is none.
            env = {**env, "HOME": str(self.container_home())}

        # Environment goes through a file rather than --env flags so that values
        # never appear in the host process list.
        env_file = None
        if self.paths is not None:
            # SG-R7: under `.multiagents`, which the container can write. Never
            # through a link or into a FIFO an agent left at that path.
            env_file = self.env_file(env.get("MULTIAGENTS_AGENT_ID", "run"))
            self.paths.data.mkdir(parents=True, exist_ok=True)
            gitops._write_beneath(
                self.paths.data, env_file.parent.relative_to(self.paths.data).parts,
                env_file.name,
                "".join(f"{k}={v}\n" for k, v in env.items()
                        if "\n" not in str(v)).encode(),
                mode=0o600, create=True)

        command = ["docker", "exec", "-i", "--workdir", str(cwd),
                   "--user", f"{os.getuid()}:{os.getgid()}"]
        if env_file is not None:
            command += ["--env-file", str(env_file)]
        else:
            for key, value in env.items():
                command += ["--env", f"{key}={value}"]
        command.append(self.container)

        pid_file = self._pid_file(env)
        if run_dir is not None:
            return self._start_wrapped(command, argv, run_dir, deadline, pid_file, env)
        # Record the agent's container-side pid so it can actually be stopped.
        command += _recording_pid(pid_file, argv)

        proc = await asyncio.create_subprocess_exec(
            *command,
            limit=STREAM_LIMIT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return DockerHandle(proc.pid, proc, self.container, pid_file)

    def _start_wrapped(self, command: list[str], argv: list[str], run_dir: Path,
                       deadline: float, pid_file: Path, env: dict[str, str]) -> FollowHandle:
        """SV-R1 on the host: the wrapper runs in the container, under its
        `python3`, and writes to the run dir on the shared bind mount.

        The `docker exec` client stays attached but holds no pipe — every one
        of its streams is /dev/null — so it neither carries the agent's output
        nor dies with this server (its own session). It is the pid recorded
        for the node: it lives exactly as long as the wrapper inside, which
        writes `exit_status` before it exits, the same guarantee the local
        executor's wrapper pid gives. Stopping goes through `docker exec`,
        since killing the client does not stop what it started.
        """
        # SG-R7: the run dir is where the container writes; nothing in it is
        # made, removed or written through a link, and no FIFO blocks here.
        base, parts = gitops.beneath(run_dir)
        os.close(gitops._open_beneath(base, parts, create=True))
        gitops._unlink_beneath(base, parts, "exit_status")
        offset = _turn_start_beneath(base, parts, "output.ndjson")
        # Entered through `sh`, as every other command here enters the
        # container. The PATH it sees is the host's, carried in `env` for
        # the agent's sake (its launcher is mounted at its host path), and a
        # `python3` found first on it may be a host interpreter mounted in
        # along with a home directory. The wrapper needs only the stdlib, so
        # the image's own interpreter is preferred; PATH is the fallback.
        _, flag, source, *rest = wrapper_argv("python3", run_dir, deadline, pid_file,
                                              argv, inline=True)
        command = command + ["sh", "-c", _WRAPPER_ENTRY.format(flag=flag), source, *rest]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        agent_id = env.get("MULTIAGENTS_AGENT_ID", "run")
        return FollowHandle(pid=proc.pid, run_dir=run_dir, offset=offset,
                            pid_start=procs.start_time(proc.pid), _proc=proc,
                            stopper=lambda grace: self.kill_detached(agent_id, grace),
                            probe=self.liveness(agent_id))

    def env_file(self, agent_id: str) -> Path:
        """Where `start` writes the environment agent `agent_id` runs with."""
        return self.paths.data / "env" / f"{agent_id}.env"

    def git(self, agent_id: str) -> gitops.Git:
        """SG-R3: in the container, as the agent runs. From inside it, git
        already runs there."""
        if self.paths is None or self.inside():
            return gitops.HOST
        return ContainerGit(self, agent_id)

    def _pid_file(self, env: dict[str, str]) -> Path:
        pid_file = (self.paths.run_dir(env.get("MULTIAGENTS_AGENT_ID", "run"))
                    if self.paths is not None else Path("/tmp")) / "container.pid"
        # SG-R7: the run dir is made through no link an agent planted, every
        # part of it beneath `.multiagents` — an agent id can hold a `/`.
        if self.paths is not None:
            self.paths.data.mkdir(parents=True, exist_ok=True)
            base, parts = self.paths.data, pid_file.parent.relative_to(self.paths.data).parts
        else:
            base, parts = gitops.beneath(pid_file.parent)
        os.close(gitops._open_beneath(base, parts, create=True))
        return pid_file

    async def _start_inside(self, argv: list[str], cwd: Path, env: dict[str, str], *,
                            run_dir: Path | None = None, deadline: float = 0,
                            provider: str = "") -> Handle:
        """Start `argv` from within the container, as `docker exec` would.

        Already in the container, a start is a local one: `LocalExecutor`
        does it, and this adds only what `docker exec` would have added — the
        same pid-recording shell, so `kill_detached` from the host still
        reaches the agent. Stopped as a local process group from in here.

        A `docker exec` from the host gets two things for free that a plain
        `LocalExecutor.start` does not, because they are properties of the
        container rather than of any one launch:

        - The network/auth-proxy variables (`NETWORK_ENV_KEYS`) `run_args`
          put on the container at `docker run` time. `docker exec` inherits
          them from the container's own environment; `asyncio.create_subprocess_exec`
          with an explicit `env=` does not inherit anything, this process's
          included, and `build_env` builds `env` from a clean slate (SM-R4's
          deny-by-default), so they are silently absent from a child spawned
          in here. Filled in here from *this* process's own environment —
          which is the container's environment, since nothing else could have
          set it — and only for those names, so nothing else this process
          happens to have (a credential included) rides along. An explicit
          value already in `env` (identity, provider config) still wins.
        - `_versioned_argv`'s resolution of a versioned launcher symlink to
          the version current at spawn time (P0-R1.2): the host branch does
          it before building its `docker exec` command; this branch skipped
          it. Mount paths match the host exactly (module docstring), so the
          same resolution is safe to do from in here too.
        """
        env = dict(env)
        for key in NETWORK_ENV_KEYS:
            if key in env:
                continue
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        argv = self._versioned_argv(argv)
        env = self.adapter_env(argv, env, provider)
        if run_dir is not None:
            # The wrapper records the agent's pid where `kill_detached` reads it.
            return await LocalExecutor().start(argv, cwd, env, run_dir=run_dir,
                                               deadline=deadline,
                                               pid_file=self._pid_file(env))
        return await LocalExecutor().start(
            _recording_pid(self._pid_file(env), argv), cwd, env)
