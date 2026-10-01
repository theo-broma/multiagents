import pytest
import os
import json
import time
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent / "support"))
from d2_support import proj  # noqa: E402,F401

import multiagents.viewer as viewer
import multiagents.tmux as tmux
from multiagents.monitor import actions

class FakePaths:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.slug = "testproj"
        self._run_dir = tmp_path / "runs"
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self.tree_file = tmp_path / "tree.json"
        self.events_file = tmp_path / "events.jsonl"
        with open(self.tree_file, "w") as f:
            f.write(json.dumps({"nodes": {}}))
            
    def run_dir(self, agent_id):
        d = self._run_dir / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

# 1. Truncation bug
def test_follower_drops_lines_when_file_grows_after_truncation(tmp_path, capsys, monkeypatch):
    # TS-R2: 1 s of TM-R3's 60 s linger. The rewrite lands in the same poll
    # that sees the run terminal, so the view needs the linger's later polls
    # to notice it; their number is not what this is about.
    monkeypatch.setattr(viewer, "LINGER_SECONDS", 1)
    paths = FakePaths(tmp_path)
    agent_id = "ag-111111"
    run_dir = paths.run_dir(agent_id)
    stream_path = run_dir / "stream.jsonl"
    
    with open(stream_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "text", "text": "A" * 80}) + "\n")
        
    class FakeTree:
        def __init__(self, *args): pass
        def get(self, id): return {"status": "running"}
    monkeypatch.setattr(viewer, "Tree", FakeTree)      # not leaked (TS-R1)
    
    # We will patch fh.readline to simulate the file truncation while the viewer is running!
    import builtins
    orig_open = builtins.open
    
    file_reopened = False
    
    class FakeFile:
        def __init__(self, underlying):
            self.underlying = underlying
            self.reads = 0
            
        def tell(self): return self.underlying.tell()
        def seek(self, *args): return self.underlying.seek(*args)
        def close(self): return self.underlying.close()
        
        def readline(self):
            line = self.underlying.readline()
            if not line:
                self.reads += 1
                if self.reads == 1:
                    # EOF reached! Truncate and grow file without changing inode
                    # We can't actually keep inode identical while truncating easily in Python
                    # But we can just write to the same file descriptor to overwrite!
                    with orig_open(stream_path, "w", encoding="utf-8") as fw:
                        fw.write(json.dumps({"kind": "text", "text": "B" * 130}) + "\n")
                    # Turn off follow to exit the loop
                    viewer.Tree.get = lambda self, id: {"status": "done"}
            return line

    def mock_open(name, *args, **kwargs):
        if str(name).endswith("stream.jsonl"):
            nonlocal file_reopened
            if file_reopened:
                return orig_open(name, *args, **kwargs)
            file_reopened = True
            return FakeFile(orig_open(name, *args, **kwargs))
        return orig_open(name, *args, **kwargs)
        
    viewer.builtins_open = mock_open
    builtins.open = mock_open
    
    try:
        viewer.view_stream(paths, agent_id, follow=True)
    finally:
        builtins.open = orig_open
    
    out, err = capsys.readouterr()
    assert "--- stream truncated ---" in out, "Truncation was not detected because the file grew larger than its previous size"

# 2. Monitor action crashes with sys.exit
def test_tmux_open_sys_exits_on_bad_socket_dir(tmp_path):
    paths = FakePaths(tmp_path)
    agent_id = "ag-222222"
    sock_dir = tmux.get_sock_dir(paths)
    sock_dir.parent.mkdir(parents=True, exist_ok=True)
    sock_dir.touch()
    
    try:
        result = actions.perform(paths, "tmux_open", {"agent_id": agent_id})
        assert not result["ok"], "Should return error for bad socket dir"
    except SystemExit:
        pytest.fail("Monitor action raised SystemExit instead of returning an error")

# 3. Monitor action returns ok for foreign agent
def test_tmux_open_approves_foreign_agent(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    agent_id = "ag-333333"
    
    # monkeypatch, not assignment: a bare `tmux.check_tmux = ...` outlived this
    # test and let a later one in the same process find tmux on an empty PATH
    # (test_d2_monitor's without-tmux test, red under xdist).
    monkeypatch.setattr(tmux, "check_tmux", lambda: None)
    import subprocess
    def mock_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="ma-testproj\n")
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    result = actions.perform(paths, "tmux_open", {"agent_id": agent_id})
    assert result.get("ok") is False, "Foreign agent should be rejected"

# 4. Concurrent tmux open race condition
def test_tmux_open_concurrent_race_condition(proj):
    import threading
    from d2_support import agent_id, err_text
    aid = agent_id(1)
    proj.add_agent(aid, "running", [])

    results = []
    def run_open():
        try:
            results.append(proj.run("tmux", "open", aid))
        except BaseException as e:
            results.append(e)

    threads = [threading.Thread(target=run_open) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 2
    for r in results:
        assert not isinstance(r, BaseException), f"open raised: {r!r}"
        assert r.returncode == 0, err_text(r)
    assert proj.tmux.windows(proj.sock, proj.session).count(aid) == 1

# 5. Lone surrogate crash
def test_follower_crashes_on_lone_surrogate(tmp_path, capsys, monkeypatch):
    paths = FakePaths(tmp_path)
    agent_id = "ag-555555"
    run_dir = paths.run_dir(agent_id)
    stream_path = run_dir / "stream.jsonl"
    
    with open(stream_path, "w", encoding="utf-8") as f:
        f.write('{"kind": "text", "text": "\\ud800"}\n')
        
    class FakeTree:
        def __init__(self, *args): pass
        def get(self, id): return {"status": "TERMINAL"}
    monkeypatch.setattr(viewer, "Tree", FakeTree)      # not leaked (TS-R1)
    
    try:
        viewer.view_stream(paths, agent_id, follow=False)
    except UnicodeEncodeError:
        pytest.fail("Viewer crashed attempting to print a lone surrogate")

# 6. Broken symlink socket crash
def test_tmux_open_crashes_on_broken_symlink_socket_dir(tmp_path):
    paths = FakePaths(tmp_path)
    agent_id = "ag-666666"
    sock_dir = tmux.get_sock_dir(paths)
    sock_dir.parent.mkdir(parents=True, exist_ok=True)
    
    os.symlink("/tmp/nonexistent_path_123", str(sock_dir))
    
    try:
        tmux.cmd_tmux_open(paths, agent_id)
        pytest.fail("Should have rejected broken symlink")
    except FileExistsError:
        pytest.fail("Broken symlink caused FileExistsError crash instead of clean rejection")
    except SystemExit:
        pass

# 7. Bidi overrides escape
def test_follower_escapes_bidi_overrides(tmp_path, capsys, monkeypatch):
    paths = FakePaths(tmp_path)
    agent_id = "ag-777777"
    run_dir = paths.run_dir(agent_id)
    stream_path = run_dir / "stream.jsonl"
    
    with open(stream_path, "w", encoding="utf-8") as f:
        f.write('{"kind": "text", "text": "\\u202e"}\n')
        
    class FakeTree:
        def __init__(self, *args): pass
        def get(self, id): return {"status": "TERMINAL"}
    monkeypatch.setattr(viewer, "Tree", FakeTree)      # not leaked (TS-R1)
    
    viewer.view_stream(paths, agent_id, follow=False)
    out, err = capsys.readouterr()
    
    assert "\u202e" not in out, "Bidi override was printed unescaped, compromising terminal safety"

