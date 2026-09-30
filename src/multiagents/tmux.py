import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .paths import ProjectPaths
from .authority import state_root
from .tree import Tree


class TmuxError(Exception):
    """A refusal or failure the caller reports: the CLI maps `code` to its exit
    status and the monitor action maps it to `ok: false`. Library code never
    exits the process (TM-R2a)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def get_sock_dir(paths: ProjectPaths) -> Path:
    d = state_root() / "host-authority" / paths.slug / "tmux"
    return d

def get_sock(paths: ProjectPaths) -> Path:
    return get_sock_dir(paths) / "sock"

def check_tmux() -> None:
    if shutil.which("tmux") is None:
        raise TmuxError("tmux is missing", 3)

def _validate_id(agent_id: str):
    if not isinstance(agent_id, str) or not re.match(r"^ag-[0-9a-f]{6}(-[0-9]+)?\Z", agent_id):
        raise TmuxError(f"invalid agent id: {agent_id}", 2)

def _check_known(paths: ProjectPaths, agent_id: str) -> None:
    """An unknown agent has no tree node and no run directory (TM-R2a)."""
    if Tree(paths.tree_file, paths.events_file).get(agent_id):
        return
    run_dir = paths.run_dir(agent_id)
    # an empty directory is not a run: nothing was ever written into it
    if run_dir.is_dir() and any(run_dir.iterdir()):
        return
    raise TmuxError(f"unknown agent: {agent_id}", 2)

def _check_socket_dir(sock_dir: Path) -> None:
    # lstat, so that a broken symlink is seen and refused like any non-directory
    try:
        st = os.lstat(sock_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TmuxError(f"tmux socket directory is unusable: {exc}", 1)
    if not stat.S_ISDIR(st.st_mode):
        raise TmuxError("tmux socket directory is not a regular directory", 1)
    if st.st_uid != os.getuid():
        raise TmuxError("tmux socket directory is owned by another user", 1)
    if st.st_mode & 0o777 != 0o700:
        raise TmuxError("tmux socket directory must be mode 0700", 1)

def _output(exc: subprocess.CalledProcessError) -> str:
    parts = []
    for part in (exc.stderr, exc.output):
        if isinstance(part, bytes):
            part = part.decode("utf-8", "replace")
        if part:
            parts.append(part)
    return " ".join(parts).strip()

def _tmux(sock: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", "-S", str(sock), *args], check=True,
                              capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise TmuxError(f"tmux {args[0]} failed: {_output(exc)}", 1) from exc

def _windows(sock: Path, session: str) -> list[str]:
    cp = subprocess.run(["tmux", "-S", str(sock), "list-windows", "-t", session, "-F", "#{window_name}"],
                        capture_output=True, text=True)
    return (cp.stdout or "").splitlines()

def _attach(sock: Path, session: str, agent_id: str) -> str:
    return f"tmux -S {sock} attach -r -t {session}:{agent_id}"


def open_window(paths: ProjectPaths, agent_id: str) -> str:
    """Put a viewer for `agent_id` in the project's session; return the attach command."""
    _validate_id(agent_id)
    check_tmux()
    _check_known(paths, agent_id)
    sock_dir = get_sock_dir(paths)
    _check_socket_dir(sock_dir)
    sock = sock_dir / "sock"

    if os.path.lexists(sock):
        if sock.is_symlink() or sock.is_dir():
            raise TmuxError("tmux socket is not a regular file or socket", 1)
        cp = subprocess.run(["tmux", "-S", str(sock), "list-sessions"], capture_output=True)
        if cp.returncode != 0:
            sock.unlink(missing_ok=True)

    sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    session = f"ma-{paths.slug}"
    cmd = f"{sys.executable} -m multiagents.cli --path {paths.root} view {agent_id} --follow"

    created = False
    if not os.path.lexists(sock):
        try:
            _tmux(sock, "new-session", "-d", "-s", session, "-n", agent_id, cmd)
            created = True
        except TmuxError as exc:
            # a concurrent open got there first: the session exists, not an error
            if "duplicate session" not in str(exc):
                raise
    if not created and agent_id not in _windows(sock, session):
        try:
            _tmux(sock, "new-window", "-d", "-t", session, "-n", agent_id, cmd)
            created = True
        except TmuxError as exc:
            # ... or it created this very window in the meantime
            if agent_id not in _windows(sock, session):
                raise exc
    if created:
        _tmux(sock, "set-option", "-t", f"{session}:{agent_id}", "remain-on-exit", "off")
    return _attach(sock, session, agent_id)


def attach_cmd(paths: ProjectPaths, agent_id: str) -> str:
    _validate_id(agent_id)
    check_tmux()
    sock_dir = get_sock_dir(paths)
    _check_socket_dir(sock_dir)
    sock = sock_dir / "sock"
    session = f"ma-{paths.slug}"
    if not os.path.lexists(sock) or agent_id not in _windows(sock, session):
        raise TmuxError(f"no window for {agent_id}", 1)
    return _attach(sock, session, agent_id)


def close_window(paths: ProjectPaths, agent_id: str) -> None:
    _validate_id(agent_id)
    check_tmux()
    sock_dir = get_sock_dir(paths)
    _check_socket_dir(sock_dir)
    sock = sock_dir / "sock"
    if not os.path.lexists(sock):
        return
    session = f"ma-{paths.slug}"
    subprocess.run(["tmux", "-S", str(sock), "kill-window", "-t", f"{session}:{agent_id}"], capture_output=True)


def kill_session(paths: ProjectPaths) -> None:
    check_tmux()
    sock_dir = get_sock_dir(paths)
    _check_socket_dir(sock_dir)
    sock = sock_dir / "sock"
    if not os.path.lexists(sock):
        return
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    sock.unlink(missing_ok=True)


# The CLI layer: the one place a TmuxError becomes an exit status.

def _run_cli(fn, *args):
    try:
        return fn(*args)
    except TmuxError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.code)

def cmd_tmux_open(paths: ProjectPaths, agent_id: str) -> None:
    print(_run_cli(open_window, paths, agent_id))

def cmd_tmux_attach_cmd(paths: ProjectPaths, agent_id: str) -> None:
    print(_run_cli(attach_cmd, paths, agent_id))

def cmd_tmux_close(paths: ProjectPaths, agent_id: str) -> None:
    _run_cli(close_window, paths, agent_id)

def cmd_tmux_kill(paths: ProjectPaths) -> None:
    _run_cli(kill_session, paths)
