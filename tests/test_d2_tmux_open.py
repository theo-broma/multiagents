"""TM-R2: `multiagents tmux open|attach-cmd` — a viewer in the project's tmux session.

tmux is a fake on PATH that records its argv (tests/support/d2_support.py).
"""
from __future__ import annotations

import os
import re
import shlex
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from d2_support import agent_id, err_text, out_text, proj  # noqa: E402

AID = agent_id(1)
AID2 = agent_id(2, "3")


@pytest.fixture
def agent(proj):
    proj.add_agent(AID, "running", [])
    return AID


def opened(proj, aid=AID, **kw):
    cp = proj.run("tmux", "open", aid, **kw)
    assert cp.returncode == 0, err_text(cp)
    return cp


def window_creations(proj, aid=AID):
    return [c for c in proj.tmux.created_windows() if c["name"] == aid]


# ------------------------------------------------------------ the basics ----

def test_tm_r2_open_creates_the_session_once_with_a_window_named_for_the_agent(proj, agent):
    opened(proj)
    assert len(proj.tmux.calls_of("new-session", "new")) == 1
    assert proj.tmux.sessions(proj.sock) == [proj.session]
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


def test_tm_r2_session_name_is_ma_dash_project_slug(proj, agent):
    opened(proj)
    assert re.fullmatch(r"ma-.+-[0-9a-f]{8}", proj.session)
    assert proj.tmux.sessions(proj.sock) == [f"ma-{proj.paths.slug}"]


def test_tm_r2_window_command_is_the_view_command_with_an_absolute_interpreter(proj, agent):
    opened(proj)
    (creation,) = window_creations(proj)
    command = creation["command"]
    assert re.search(rf"\bview\s+{AID}\s+--follow\b", command), command
    first = shlex.split(command)[0]
    assert os.path.isabs(first), f"interpreter is not an absolute path: {command!r}"
    assert os.access(first, os.X_OK)


def test_tm_r2_every_tmux_invocation_carries_the_private_socket(proj, agent):
    opened(proj)
    proj.run("tmux", "attach-cmd", AID)
    proj.run("tmux", "close", AID)
    proj.run("tmux", "kill")
    assert proj.tmux.calls
    for call in proj.tmux.calls:
        assert call["sock"] == str(proj.sock), call["argv"]
        assert call["argv"][:2] == ["-S", str(proj.sock)] or "-S" in call["argv"]


def test_tm_r2_prints_the_readonly_attach_command(proj, agent):
    cp = opened(proj)
    assert proj.attach_command(AID) in out_text(cp).splitlines()
    assert "attach -r" in out_text(cp)


def test_tm_r2_open_never_attaches_and_needs_no_terminal(proj, agent):
    cp = opened(proj)                                      # stdin/out are pipes here
    assert proj.tmux.calls_of("attach", "attach-session", "a", "at") == []
    assert cp.returncode == 0


def test_tm_r2_windows_do_not_stay_open_after_their_command_exits(proj, agent):
    opened(proj)
    for call in proj.tmux.calls:
        text = " ".join(call["argv"])
        if "remain-on-exit" in text:
            assert re.search(r"remain-on-exit\s+off\b", text), text


# ------------------------------------------------------------ idempotence ----

def test_tm_r2_second_open_selects_the_existing_window_instead_of_a_new_one(proj, agent):
    first = opened(proj)
    second = opened(proj)
    assert len(window_creations(proj)) == 1
    assert len(proj.tmux.calls_of("new-session", "new")) == 1
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1
    assert out_text(first) == out_text(second)


def test_tm_r2_five_opens_still_one_window(proj, agent):
    for _ in range(5):
        opened(proj)
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


def test_tm_r2_two_agents_share_one_session_with_two_windows(proj, agent):
    proj.add_agent(AID2, "running", [])
    opened(proj, AID)
    opened(proj, AID2)
    assert len(proj.tmux.calls_of("new-session", "new")) == 1
    windows = proj.tmux.windows(proj.sock, proj.session)
    assert windows.count(AID) == 1 and windows.count(AID2) == 1


def test_tm_r2_open_after_the_window_was_closed_creates_it_again(proj, agent):
    proj.add_agent(AID2, "running", [])
    opened(proj, AID)
    opened(proj, AID2)
    assert proj.run("tmux", "close", AID).returncode == 0
    assert AID not in proj.tmux.windows(proj.sock, proj.session)
    opened(proj, AID)
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


# ------------------------------------------------------------- attach-cmd ----

def test_tm_r2_attach_cmd_prints_the_same_command_as_open(proj, agent):
    cp = opened(proj)
    calls_before = len(proj.tmux.calls)
    ac = proj.run("tmux", "attach-cmd", AID)
    assert ac.returncode == 0, err_text(ac)
    assert proj.attach_command(AID) in out_text(ac).splitlines()
    assert proj.attach_command(AID) in out_text(cp).splitlines()
    created = [c for c in proj.tmux.calls[calls_before:]
               if c["cmd"] in ("new-session", "new", "new-window", "neww", "kill-window",
                               "kill-session", "kill-server")]
    assert created == []


def test_tm_r2_attach_cmd_exits_1_when_the_window_does_not_exist(proj, agent):
    cp = proj.run("tmux", "attach-cmd", AID)
    assert cp.returncode == 1
    assert proj.tmux.calls_of("new-session", "new", "new-window", "neww") == []
    assert not proj.sock.exists()


def test_tm_r2_attach_cmd_exits_1_for_another_agents_window(proj, agent):
    proj.add_agent(AID2, "running", [])
    opened(proj, AID)
    assert proj.run("tmux", "attach-cmd", AID2).returncode == 1
    assert proj.tmux.windows(proj.sock, proj.session).count(AID2) == 0


def test_tm_r2_attach_cmd_exits_1_after_the_window_is_closed(proj, agent):
    proj.add_agent(AID2, "running", [])
    opened(proj, AID)
    opened(proj, AID2)
    proj.run("tmux", "close", AID)
    assert proj.run("tmux", "attach-cmd", AID).returncode == 1
    assert proj.run("tmux", "attach-cmd", AID2).returncode == 0


# ------------------------------------------------------------ id checking ----

@pytest.mark.parametrize("sub", ["open", "attach-cmd", "close"])
@pytest.mark.parametrize("bad", ["../x", "ag-zzz", "ag-abc123\n", "ag-abc123;kill-server",
                                 "ag-abc123:0", "-t", "ag-ABCDEF", "ag-abc123-x", "ma-x:0"])
def test_tm_r2_malformed_ids_exit_2_and_never_reach_tmux(proj, sub, bad):
    cp = proj.run("tmux", sub, bad)
    assert cp.returncode == 2, (cp.returncode, err_text(cp))
    assert proj.tmux.calls == []
    assert err_text(cp).strip()


# --------------------------------------------------------- socket location ----

def test_tm_r2_socket_directory_is_0700_under_host_authority(proj, agent):
    opened(proj)
    assert proj.sock_dir.is_dir()
    assert stat.S_IMODE(proj.sock_dir.stat().st_mode) == 0o700
    assert proj.sock_dir.stat().st_uid == os.getuid()
    assert proj.sock_dir.relative_to(proj.state / "host-authority" / proj.slug)


def test_tm_r2_socket_is_not_anywhere_under_the_projects_dot_multiagents(proj, agent):
    opened(proj)
    (call,) = proj.tmux.calls_of("new-session", "new")
    sock = Path(call["sock"]).resolve()
    assert proj.paths.data.resolve() not in sock.parents
    assert proj.root.resolve() not in sock.parents
    assert not any(p.name == "sock" for p in proj.paths.data.rglob("*"))
    assert sock == proj.sock


def test_tm_r2_socket_directory_honours_a_permissive_umask(proj, agent):
    old = os.umask(0)
    try:
        opened(proj)
    finally:
        os.umask(old)
    assert stat.S_IMODE(proj.sock_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o770, 0o707, 0o701, 0o710, 0o750])
def test_tm_r2_existing_socket_directory_with_wider_permissions_is_refused(proj, agent, mode):
    proj.sock_dir.mkdir(parents=True)
    proj.sock_dir.chmod(mode)
    cp = proj.run("tmux", "open", AID)
    assert cp.returncode not in (0, 3), (cp.returncode, err_text(cp))
    assert err_text(cp).strip()
    assert proj.tmux.calls_of("new-session", "new", "new-window", "neww") == []
    assert not proj.sock.exists()


def test_tm_r2_wide_socket_directory_is_refused_by_attach_cmd_and_close_too(proj, agent):
    proj.sock_dir.mkdir(parents=True)
    proj.sock_dir.chmod(0o777)
    for sub in (("attach-cmd", AID), ("close", AID), ("kill",)):
        cp = proj.run("tmux", *sub)
        assert cp.returncode not in (0, 3), (sub, cp.returncode, err_text(cp))
    assert proj.tmux.calls == []


def test_tm_r2_a_0700_socket_directory_left_by_an_earlier_run_is_reused(proj, agent):
    proj.sock_dir.mkdir(parents=True)
    proj.sock_dir.chmod(0o700)
    opened(proj)
    assert stat.S_IMODE(proj.sock_dir.stat().st_mode) == 0o700


# ------------------------------------------------------------ stale sockets ----

def _stale_socket(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()                                              # file stays, nobody listens
    assert path.exists()


def test_tm_r2_a_stale_socket_is_replaced(proj, agent):
    _stale_socket(proj.sock)
    opened(proj)
    (call,) = proj.tmux.calls_of("new-session", "new")
    assert call["sock_exists"] is False, "the dead socket was still in the way when the server started"
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1
    assert stat.S_ISSOCK(proj.sock.stat().st_mode)


def test_tm_r2_a_stale_regular_file_at_the_socket_path_is_replaced(proj, agent):
    proj.sock_dir.mkdir(parents=True)
    proj.sock_dir.chmod(0o700)
    proj.sock.write_text("junk")
    opened(proj)
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


def test_tm_r2_a_live_socket_is_never_removed_by_a_second_open(proj, agent):
    proj.add_agent(AID2, "running", [])
    opened(proj, AID)
    opened(proj, AID2)
    for call in proj.tmux.calls_of("new-session", "new"):
        assert call["sock_exists"] is False
    assert len(proj.tmux.calls_of("new-session", "new")) == 1
    assert len(proj.tmux.windows(proj.sock, proj.session)) == 2


def test_tm_r2_stale_socket_handling_does_not_touch_the_default_server(proj, agent):
    _stale_socket(proj.sock)
    opened(proj)
    assert all(c["sock"] == str(proj.sock) for c in proj.tmux.calls)
