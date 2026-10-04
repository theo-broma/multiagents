"""The fixture agent of NC-R70 does what its task says (test infrastructure).

These tests are about `tests/nc_fixture/agent.py` itself, run directly as a
subprocess the way a provider CLI is: they are expected to PASS before any
scheduler exists, because every M2 contract test leans on them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import FixtureProvider, alive, task  # noqa: E402


def run(fx: FixtureProvider, text: str, *argv: str, cwd: Path, wait=True):
    p = subprocess.Popen([fx.entry["bin"], *argv], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, cwd=cwd)
    p.stdin.write(text)
    p.stdin.close()
    if wait:
        out = p.stdout.read()
        p.wait(timeout=20)
        return p, [json.loads(x) for x in out.splitlines() if x.strip()]
    return p, []


@pytest.fixture
def fx(tmp_path):
    (tmp_path / "w").mkdir()
    return FixtureProvider(tmp_path, "fxa")


def texts(events):
    return [e["part"]["text"] for e in events if e["type"] == "text"]


def test_nc_fixture_emits_a_session_id_and_finishes_done(fx, tmp_path):
    p, ev = run(fx, task("a", session="ses_x"), cwd=tmp_path / "w")
    assert p.returncode == 0
    assert {e["sessionID"] for e in ev} == {"ses_x"}
    assert texts(ev)[-1] == "done"
    assert fx.by_tag("a")[0]["session"] == "ses_x"
    assert fx.done_tags() == ["a"]


def test_nc_fixture_resumes_the_session_it_is_given(fx, tmp_path):
    p, ev = run(fx, task("a", session="ignored"), "-s", "ses_old", cwd=tmp_path / "w")
    assert {e["sessionID"] for e in ev} == {"ses_old"}
    assert fx.by_tag("a")[0]["resume"] == "ses_old"


def test_nc_fixture_commits_files_and_exits_done(fx, tmp_path):
    w = tmp_path / "w"
    subprocess.run(["git", "init", "-q"], cwd=w, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "i"], cwd=w, check=True)
    p, _ = run(fx, task("a", write={"d/f.txt": "hi"}, commit="add f"), cwd=w)
    assert p.returncode == 0 and (w / "d/f.txt").read_text() == "hi"
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=w, capture_output=True,
                         text=True).stdout.strip()
    assert log == "add f"


def test_nc_fixture_crash_dies_without_a_final_text(fx, tmp_path):
    p, ev = run(fx, task("a", crash=True), cwd=tmp_path / "w")
    assert p.returncode == 1 and "done" not in texts(ev) and fx.done() == []


def test_nc_fixture_hang_ends_on_sigterm(fx, tmp_path):
    p, _ = run(fx, task("a", hang=True), cwd=tmp_path / "w", wait=False)
    fx_calls = lambda: fx.by_tag("a")
    deadline = time.time() + 10
    while not fx_calls() and time.time() < deadline:
        time.sleep(0.05)
    assert alive(p.pid)
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)
    assert p.returncode != 0 and fx.done() == []


def test_nc_fixture_ignore_term_survives_sigterm_until_sigkill(fx, tmp_path):
    p, _ = run(fx, task("a", hang=True, ignore_term=True), cwd=tmp_path / "w", wait=False)
    deadline = time.time() + 10
    while not fx.by_tag("a") and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    p.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    assert p.poll() is None
    p.kill()
    p.wait(timeout=10)


def test_nc_fixture_gate_holds_until_opened(fx, tmp_path):
    p, _ = run(fx, task("a", gate="g"), cwd=tmp_path / "w", wait=False)
    deadline = time.time() + 10
    while not fx.by_tag("a") and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assert p.poll() is None and fx.done() == []
    fx.open_gate("g")
    p.wait(timeout=10)
    assert p.returncode == 0 and fx.done_tags() == ["a"]


def test_nc_fixture_reports_whether_a_watched_pid_is_alive(fx, tmp_path):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        run(fx, task("a", watch_pid=sleeper.pid), cwd=tmp_path / "w")
        assert fx.by_tag("a")[0]["watch_alive"] is True
    finally:
        sleeper.kill()
        sleeper.wait()
    run(fx, task("b", watch_pid=sleeper.pid), cwd=tmp_path / "w")
    assert fx.by_tag("b")[0]["watch_alive"] is False


def test_nc_fixture_reads_the_mcp_environment_from_its_provider_config(fx, tmp_path):
    cfg = tmp_path / "oc.json"
    cfg.write_text(json.dumps({"mcp": {"multiagents": {
        "environment": {"MULTIAGENTS_RPC_TOKEN": "tok", "MULTIAGENTS_AGENT_ID": "ag-1"}}}}))
    env = dict(os.environ, OPENCODE_CONFIG=str(cfg))
    p = subprocess.Popen([fx.entry["bin"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, cwd=tmp_path / "w", env=env)
    p.stdin.write(task("a"))
    p.stdin.close()
    p.stdout.read()
    p.wait(timeout=10)
    assert fx.by_tag("a")[0]["mcp_env"]["MULTIAGENTS_RPC_TOKEN"] == "tok"


def test_nc_fixture_runs_under_todays_runner_as_a_provider(tmp_path, monkeypatch):
    """The provider block the fixture builds is accepted by today's Runner: a
    legacy (gate off) start runs it to `done` with the session id it emitted,
    and the MCP registration it reads carries the agent's identity."""
    from nc_fixture.world import World, legacy_start
    w = World(tmp_path, monkeypatch, gate=False)
    try:
        run_id, _ = legacy_start(w, "spawner", task("A", session="ses_q"))
        w.until(lambda: w.fx.done_tags() == ["A"], 60, what="the fixture run to end")
        w.until(lambda: w.tree_nodes()[run_id]["status"] == "done", 30, what="done in the tree")
        assert w.tree_nodes()[run_id]["session_id"] == "ses_q"
        assert w.fx.by_tag("A")[0]["mcp_env"].get("MULTIAGENTS_AGENT_ID") == run_id
    finally:
        w.close()
