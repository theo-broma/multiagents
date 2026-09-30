"""TM-R3 (lifecycle and cleanup) and TM-R5 (no tmux)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from d2_support import agent_id, err_text, out_text, proj  # noqa: E402

AID = agent_id(1)
AID2 = agent_id(2)
REAL_TMUX = shutil.which("tmux")


def two_windows(proj):
    proj.add_agent(AID, "running", [])
    proj.add_agent(AID2, "running", [])
    for a in (AID, AID2):
        assert proj.run("tmux", "open", a).returncode == 0


def target_of(call):
    r = call["rest"]
    return r[r.index("-t") + 1].lstrip("=")


# ------------------------------------------------------------------ close ----

def test_tm_r3_close_removes_exactly_one_window(proj):
    two_windows(proj)
    cp = proj.run("tmux", "close", AID)
    assert cp.returncode == 0, err_text(cp)
    kills = proj.tmux.calls_of("kill-window", "killw")
    assert len(kills) == 1
    assert target_of(kills[0]) == f"{proj.session}:{AID}"
    assert kills[0]["sock"] == str(proj.sock)
    assert AID not in proj.tmux.windows(proj.sock, proj.session)
    assert AID2 in proj.tmux.windows(proj.sock, proj.session)
    assert proj.tmux.calls_of("kill-session", "kill-server") == []


def test_tm_r3_close_twice_does_not_close_something_else(proj):
    two_windows(proj)
    proj.run("tmux", "close", AID)
    proj.run("tmux", "close", AID)
    assert AID2 in proj.tmux.windows(proj.sock, proj.session)
    assert proj.tmux.sessions(proj.sock) == [proj.session]


def test_tm_r3_close_without_any_session_creates_nothing(proj):
    proj.add_agent(AID, "running", [])
    proj.run("tmux", "close", AID)
    assert proj.tmux.calls_of("new-session", "new", "new-window", "neww") == []
    assert not proj.sock.exists()


def test_tm_r3_close_does_not_touch_the_agent(proj):
    two_windows(proj)
    proj.run("tmux", "close", AID)
    from multiagents.tree import Tree
    node = Tree(proj.paths.tree_file, proj.paths.events_file).get(AID)
    assert node.status == "running"


# ------------------------------------------------------------------- kill ----

def test_tm_r3_kill_removes_the_session_and_the_socket(proj):
    two_windows(proj)
    cp = proj.run("tmux", "kill")
    assert cp.returncode == 0, err_text(cp)
    kills = proj.tmux.calls_of("kill-session", "kill-server")
    assert kills and all(k["sock"] == str(proj.sock) for k in kills)
    for k in proj.tmux.calls_of("kill-session"):
        assert target_of(k) == proj.session
    assert proj.tmux.sessions(proj.sock) == []
    assert not proj.sock.exists()


def test_tm_r3_kill_removes_a_stale_socket_too(proj):
    import socket
    proj.sock_dir.mkdir(parents=True)
    proj.sock_dir.chmod(0o700)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(proj.sock))
    s.close()
    proj.run("tmux", "kill")
    assert not proj.sock.exists()


def test_tm_r3_kill_touches_only_this_projects_socket(proj, tmp_path):
    # A second project has its own server; killing the first must not reach it.
    other = tmp_path / "other"
    other.mkdir()
    from multiagents.paths import ProjectPaths
    op = ProjectPaths(other)
    op.ensure()
    from multiagents.tree import Node, Tree
    Tree(op.tree_file, op.events_file).add(
        Node(id=AID, agent="w", provider="fake", model="m", parent=None, depth=1, status="running"))
    two_windows(proj)
    cp = subprocess.run([sys.executable, "-m", "multiagents.cli", "--path", str(other),
                         "tmux", "open", AID], capture_output=True, env=proj.env(), cwd=other)
    assert cp.returncode == 0, err_text(cp)
    other_sock = proj.state / "host-authority" / op.slug / "tmux" / "sock"
    assert other_sock.exists() and other_sock != proj.sock
    proj.run("tmux", "kill")
    assert not proj.sock.exists()
    assert other_sock.exists()
    assert proj.tmux.windows(other_sock, f"ma-{op.slug}") == [AID]


def test_tm_r3_kill_with_no_server_does_not_start_one_or_use_the_default(proj):
    proj.run("tmux", "kill")
    assert proj.tmux.calls_of("new-session", "new", "new-window", "neww") == []
    assert all(c["sock"] == str(proj.sock) for c in proj.tmux.calls)


def test_tm_r3_open_works_again_after_kill(proj):
    two_windows(proj)
    proj.run("tmux", "kill")
    cp = proj.run("tmux", "open", AID)
    assert cp.returncode == 0, err_text(cp)
    assert proj.tmux.windows(proj.sock, proj.session).count(AID) == 1


def test_tm_r3_neither_command_signals_processes_or_touches_the_agent(proj):
    two_windows(proj)
    from multiagents.tree import Tree
    tree = Tree(proj.paths.tree_file, proj.paths.events_file)
    proj.run("tmux", "close", AID)
    proj.run("tmux", "kill")
    assert tree.get(AID).status == "running" and tree.get(AID2).status == "running"


# ------------------------------------------- process survival (real tmux) ----

@pytest.mark.skipif(REAL_TMUX is None, reason="tmux is not installed")
def test_tm_r3_killing_the_session_leaves_the_agent_process_alive(proj, tmp_path):
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    real_sock = proj.sock
    env = proj.env()
    env["PATH"] = os.path.dirname(REAL_TMUX) + os.pathsep + "/usr/bin:/bin"
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(tmp_path)                     # belt and braces: never the user's dir
    try:
        proj.add_agent(AID, "running", [{"kind": "text", "text": "hi"}], pid=sleeper.pid)
        cp = proj.run("tmux", "open", AID, env=env)
        assert cp.returncode == 0, err_text(cp)
        listing = subprocess.run([REAL_TMUX, "-S", str(real_sock), "list-windows", "-t",
                                  proj.session, "-F", "#{window_name}"],
                                 capture_output=True, env=env)
        assert AID in out_text(listing).split(), out_text(listing) + err_text(listing)

        assert proj.run("tmux", "kill", env=env).returncode == 0
        gone = subprocess.run([REAL_TMUX, "-S", str(real_sock), "list-sessions"],
                              capture_output=True, env=env)
        assert gone.returncode != 0                        # the server is really gone
        assert not real_sock.exists()
        time.sleep(0.5)
        assert sleeper.poll() is None, "killing the tmux session killed the agent"
        os.kill(sleeper.pid, 0)
        from multiagents.tree import Tree
        assert Tree(proj.paths.tree_file, proj.paths.events_file).get(AID).status == "running"
    finally:
        subprocess.run([REAL_TMUX, "-S", str(real_sock), "kill-server"],
                       capture_output=True, env=env)
        sleeper.kill()
        sleeper.wait()


@pytest.mark.skipif(REAL_TMUX is None, reason="tmux is not installed")
def test_tm_r3_closing_one_real_window_leaves_the_agent_and_other_window(proj, tmp_path):
    sleeper = subprocess.Popen(["sleep", "300"], start_new_session=True)
    env = proj.env()
    env["PATH"] = os.path.dirname(REAL_TMUX) + os.pathsep + "/usr/bin:/bin"
    env["TMUX_TMPDIR"] = str(tmp_path)
    try:
        proj.add_agent(AID, "running", [], pid=sleeper.pid)
        proj.add_agent(AID2, "running", [])
        for a in (AID, AID2):
            assert proj.run("tmux", "open", a, env=env).returncode == 0
        assert proj.run("tmux", "close", AID, env=env).returncode == 0
        listing = subprocess.run([REAL_TMUX, "-S", str(proj.sock), "list-windows", "-t",
                                  proj.session, "-F", "#{window_name}"],
                                 capture_output=True, env=env)
        names = out_text(listing).split()
        assert AID2 in names and AID not in names
        assert sleeper.poll() is None
        assert proj.run("tmux", "attach-cmd", AID, env=env).returncode == 1
    finally:
        subprocess.run([REAL_TMUX, "-S", str(proj.sock), "kill-server"],
                       capture_output=True, env=env)
        sleeper.kill()
        sleeper.wait()


# ------------------------------------------------------------- no tmux ----

@pytest.mark.parametrize("sub", [("open", AID), ("attach-cmd", AID), ("close", AID), ("kill",)])
def test_tm_r5_tmux_commands_exit_3_naming_tmux_when_it_is_missing(proj, sub):
    proj.add_agent(AID, "running", [])
    cp = proj.run("tmux", *sub, tmux=False)               # PATH is empty
    assert cp.returncode == 3, (cp.returncode, err_text(cp))
    assert "tmux" in (err_text(cp) + out_text(cp)).lower()
    assert "Traceback" not in err_text(cp)


def test_tm_r5_missing_tmux_creates_no_socket_directory(proj):
    proj.add_agent(AID, "running", [])
    proj.run("tmux", "open", AID, tmux=False)
    assert not proj.sock_dir.exists()


def test_tm_r5_path_with_directories_but_no_tmux_is_also_missing(proj, tmp_path):
    (tmp_path / "empty-bin").mkdir()
    proj.add_agent(AID, "running", [])
    cp = proj.run("tmux", "open", AID, path=str(tmp_path / "empty-bin"))
    assert cp.returncode == 3


def test_tm_r5_view_still_works_with_an_empty_path(proj):
    proj.add_agent(AID, "done", [{"kind": "text", "text": "still-works"}])
    cp = proj.run("view", AID, "--no-follow", tmux=False)
    assert cp.returncode == 0
    assert "still-works" in out_text(cp)
