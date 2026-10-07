"""D1 review findings (ag-de331d, 8 findings) as black-box tests.

Contract: `context/specs/d1-limit-notices-contract.md` (LN-C1..C7). Each test
is named `test_d1_f<N>_…` after the review finding it turns into a check, and
cites the clause it is grounded in. Written red against af6d413.

Harness patterns are those of `test_d1_limit_notices.py`: a loaded project, real
`Runner` instances over the same `.multiagents/`, a fake provider CLI, the
public `limit_hit` / `limit_cleared` events, `wait_for_any`, the monitor
snapshot, and the `DockerExecutor.oom_kill_count()` seam for LN-C5.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import gitops, runner as runner_mod  # noqa: E402
from multiagents.config import load  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.monitor.snapshot import snapshot  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node  # noqa: E402

from test_d1_limit_notices import (  # noqa: E402,F401
    _assert_hit, _events, _known_provider_headroom, _project, _try_start,
)


def _node(r, node_id, status="running", **kw):
    r.tree.add(Node(id=node_id, agent="worker", provider="fake", model="m1",
                    parent=None, depth=1, status=status, **kw))


def _alerts(r):
    return [str(a) for a in snapshot(r.paths, r.config, with_scripts=False)["alerts"]]


# ---------------------------------------------------------------------------
# a docker executor whose only fake part is `docker exec` itself


class _FakeDocker(DockerExecutor):
    async def start(self, argv, cwd, env, **kwargs):
        return await LocalExecutor.start(self, argv, cwd, env, **kwargs)

    async def _start_wrapped(self, argv, cwd, env, run_dir, deadline, pid_file):
        return await LocalExecutor._start_wrapped(self, argv, cwd, env,
                                                  run_dir, deadline, pid_file)

    def preflight(self):
        return []

    def git(self, agent_id):
        return gitops.HOST

    def inside(self):
        return False

    def liveness(self, agent_id):
        return lambda: False

    def kill_detached(self, agent_id, grace):
        return None


def _docker_project(tmp_path, monkeypatch, *, kill_delay=2, sleeper_delay=0):
    """Agents `worker` (dies by SIGKILL, exit 137) and `sleeper` (lives
    `sleeper_delay` seconds, exits 0), both on a docker executor limited to 64m."""
    r, project_file, _ = _project(tmp_path, monkeypatch)
    project_file.write_text(
        "team: ''\nlimits:\n  retry_silent_failure_under_seconds: 0\n"
        "executor:\n  kind: docker\n  docker:\n    memory: 64m\n")
    providers = {
        "fake": h.fake_cli(tmp_path, "kill", events=[{"type": "text", "text": "before kill"}],
                           exit_code=137, delay=kill_delay),
        "slow": h.fake_cli(tmp_path, "slow", events=[{"type": "text", "text": "still here"}],
                           exit_code=0, delay=sleeper_delay),
    }
    (r.paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": providers}))
    (r.paths.config / "agents.yaml").write_text(
        "agents:\n  worker:\n    provider: fake\n    model: m1\n"
        "  sleeper:\n    provider: slow\n    model: m1\n")
    r = Runner(r.paths, load(r.paths, seed=False))
    fake = _FakeDocker({}, r.paths, r.providers)
    monkeypatch.setattr(runner_mod, "get_executor", lambda *a, **k: fake)
    return r, project_file


def _hits_for(r, node_id):
    return [e for e in _events(r, "limit_hit") if e.get("node") == node_id]


# ---------------------------------------------------------------------------
# F1 — LN-C5: sole occupancy is judged across Runner instances


def test_d1_f1_oom_increase_with_a_sibling_in_another_runner_is_kill_uncertain(tmp_path, monkeypatch):
    """LN-C5: `killed` needs this to be the only process the *runner* started
    that was alive in the container. A sibling started by a second Runner
    (another server process, same project and container) is a sibling."""
    # TS-R2: the sibling outlives the worker by 2 s rather than 4 s.
    r_a, project_file = _docker_project(tmp_path, monkeypatch, kill_delay=1, sleeper_delay=3)
    r_b = Runner(r_a.paths, load(r_a.paths, seed=False))
    counter = [10]
    monkeypatch.setattr(DockerExecutor, "oom_kill_count", lambda self: counter[0], raising=False)

    async def scenario():
        sibling = (await r_a.start("sleeper", "long-lived sibling"))["agent_id"]
        killed = (await r_b.start("worker", "gets killed"))["agent_id"]
        assert r_a.tree.get(sibling).status == "running"
        counter[0] = 11
        await asyncio.wait_for(r_b.runs[killed].done.wait(), 10)
        await asyncio.wait_for(r_a.runs[sibling].done.wait(), 15)
        return killed

    killed = asyncio.run(scenario())
    result = json.loads((r_b.paths.run_dir(killed) / "result.json").read_text())
    assert result["exit_code"] == 137, result
    hits = _hits_for(r_b, killed)
    assert len(hits) == 1, hits
    assert hits[0]["effect"] == "kill_uncertain" and hits[0]["key"] == "process.sigkill", hits[0]
    assert hits[0]["source"] is None
    assert not [e for e in _events(r_b, "limit_hit") if e.get("key") == "executor.docker.memory"]


# ---------------------------------------------------------------------------
# F2 — LN-C5: a run adopted after a server restart is still judged


def test_d1_f2_sigkill_of_an_adopted_docker_run_emits_kill_uncertain(tmp_path, monkeypatch):
    """LN-C5: an agent process that dies by SIGKILL (exit 137) in the container
    is reported. The server that launched it went away, so the run's counter
    baseline is unknown: the notice is `kill_uncertain`, never absent."""
    r_old, _ = _docker_project(tmp_path, monkeypatch, kill_delay=3)
    monkeypatch.setattr(DockerExecutor, "oom_kill_count", lambda self: 10, raising=False)

    async def scenario():
        node_id = (await r_old.start("worker", "outlives its server"))["agent_id"]
        await r_old.shutdown(detach=True)          # SV-R3: the agent keeps running
        r_new = Runner(r_old.paths, load(r_old.paths, seed=False))
        adopted = await r_new.adopt()
        assert node_id in adopted, (adopted, r_new.tree.get(node_id))
        await asyncio.wait_for(r_new.runs[node_id].done.wait(), 15)
        return node_id, r_new

    node_id, r_new = asyncio.run(scenario())
    assert r_new.tree.get(node_id).status != "running"
    hits = _hits_for(r_new, node_id)
    assert len(hits) == 1, (hits, _events(r_new, "adopted"))
    assert hits[0]["effect"] == "kill_uncertain" and hits[0]["key"] == "process.sigkill", hits[0]
    assert hits[0]["source"] is None


# ---------------------------------------------------------------------------
# F3 — LN-C4: dedup state cannot be forged by an agent-writable file


def _tree_json(r):
    return json.loads(r.paths.tree_file.read_text())


def test_d1_f3_forged_active_entry_in_tree_json_does_not_suppress_a_real_limit_hit(tmp_path, monkeypatch):
    """LN-C4: `events.jsonl` is the history; an entry an agent wrote into the
    tree file for (limits.max_concurrent, tree) must not silence the next real
    refusal. The forged block is a byte-copy of what a genuine hit leaves
    behind, so no format is assumed."""
    donor, _, _ = _project(tmp_path / "donor", monkeypatch, project_lines=["max_concurrent: 1"])
    _node(donor, "ag-existing")
    before = _tree_json(donor)
    assert isinstance(_try_start(donor), RuntimeError)
    assert len(_events(donor, "limit_hit")) == 1
    after = _tree_json(donor)
    forged = {k: v for k, v in after.items() if k != "nodes" and before.get(k) != v}
    assert forged, "a genuine hit left no shared state at all; fixture is wrong"

    victim, _, _ = _project(tmp_path / "victim", monkeypatch, project_lines=["max_concurrent: 1"])
    _node(victim, "ag-existing")
    data = _tree_json(victim)
    data.update(forged)
    victim.paths.tree_file.write_text(json.dumps(data))
    assert not _events(victim, "limit_hit")

    result = _try_start(victim)
    assert isinstance(result, RuntimeError), result
    _assert_hit(victim, "limits.max_concurrent", 1, "refused")


# ---------------------------------------------------------------------------
# F4 — LN-C6 commit_fix_attempts: exhaustion includes "zero were allowed"


def test_d1_f4_zero_commit_fix_attempts_with_a_refusing_hook_emits_limit_hit(tmp_path, monkeypatch):
    from test_commit_identity_r5 import FIRST, NO_FIX, fake_provider, install_hook, run_to_end
    provider, _ = fake_provider(tmp_path, [FIRST, NO_FIX])
    root = h.make_git_repo(tmp_path / "project")
    paths = ProjectPaths(root)
    paths.ensure()
    project_file = paths.config / "project.yaml"
    project_file.write_text("team: ''\nlimits:\n  commit_fix_attempts: 0\n")
    (paths.config / "agents.yaml").write_text(
        "agents:\n  worker:\n    provider: fake\n    model: m1\n    silence_timeout: 120\n")
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fake": provider}}))
    install_hook(root)
    h.as_root(monkeypatch)
    r = Runner(paths, load(paths, seed=False))
    agent_id = run_to_end(r, timeout=20)
    assert agent_id
    assert not [e for e in _events(r, "commit_fix_attempt") if e.get("agent") == agent_id], \
        "zero attempts allowed, yet a fix turn ran"
    hit = _assert_hit(r, "limits.commit_fix_attempts", 0, "stopped",
                      {"layer": "project", "file": str(project_file.resolve()), "line": 3})
    assert hit["node"] == agent_id


# ---------------------------------------------------------------------------
# F5 — LN-C4: "or the pause clears" ends a deferral notice


def _defer_on_reserve(tmp_path, monkeypatch, blind_cooldown=None):
    r, project_file, _ = _project(tmp_path, monkeypatch)
    extra = f"  blind_cooldown_seconds: {blind_cooldown}\n" if blind_cooldown else ""
    project_file.write_text("team: ''\nlimits: {}\nbudget:\n  reserve_headroom: 0.2\n"
                            "  reserve: true\n" + extra)
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "fake": budget_mod.Budget("fake", known=True, headroom=0.1)})
    r = Runner(r.paths, load(r.paths, seed=False))
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("deferred"), result
    _assert_hit(r, "budget.reserve_headroom", 0.2, "deferred")
    assert r.tree.pause_state(), "the deferral did not pause the tree"
    for entry in r.tree.read()["deferred"]:      # nothing left to restart on its own
        r.tree.drop_deferred(entry["id"])
    return r


def _cleared(r, key):
    return [e for e in _events(r, "limit_cleared") if e.get("key") == key]


def test_d1_f5_manual_resume_of_the_pause_clears_the_deferral_notice(tmp_path, monkeypatch):
    r = _defer_on_reserve(tmp_path, monkeypatch)
    r.tree.resume("operator lifted it")
    asyncio.run(r.wait_for_any(None, 0))          # any later poll of the runner
    cleared = _cleared(r, "budget.reserve_headroom")
    assert len(cleared) == 1, _events(r, "limit_cleared")
    assert cleared[0]["scope"] == "tree" and cleared[0]["count"] == 1
    assert not any("budget.reserve_headroom" in a for a in _alerts(r)), _alerts(r)


def test_d1_f5_expired_pause_clears_the_deferral_notice(tmp_path, monkeypatch):
    r = _defer_on_reserve(tmp_path, monkeypatch, blind_cooldown=1)
    time.sleep(1.3)
    asyncio.run(r.wait_for_any(None, 0))
    assert not r.tree.pause_state()
    cleared = _cleared(r, "budget.reserve_headroom")
    assert len(cleared) == 1, _events(r, "limit_cleared")
    assert not any("budget.reserve_headroom" in a for a in _alerts(r)), _alerts(r)


def test_d1_f5_manual_resume_clears_the_wind_down_notice(tmp_path, monkeypatch):
    from multiagents import tree as tree_mod
    r, project_file, _ = _project(tmp_path, monkeypatch)
    project_file.write_text(
        "team: ''\nlimits:\n  wind_down_seconds: 60\n"
        "budget:\n  burn_min_span_seconds: 0\n  burn_min_samples: 2\n")
    clock = [time.time() - 30]
    monkeypatch.setattr(tree_mod, "now", lambda: clock[0])
    r.tree.note_headroom("fake", 0.2)
    clock[0] += 30
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "fake": budget_mod.Budget("fake", known=True, headroom=0.1)})
    r = Runner(r.paths, load(r.paths, seed=False))
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("deferred"), result
    _assert_hit(r, "limits.wind_down_seconds", 60, "deferred")
    for entry in r.tree.read()["deferred"]:
        r.tree.drop_deferred(entry["id"])
    r.tree.resume("operator lifted it")
    asyncio.run(r.wait_for_any(None, 0))
    assert len(_cleared(r, "limits.wind_down_seconds")) == 1, _events(r, "limit_cleared")


# ---------------------------------------------------------------------------
# F6 — LN-C3/C4: a caller sees each notice once, from its own creation


def test_d1_f6_first_wait_returns_a_hit_and_clear_that_happened_since_the_caller_began(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    _node(r, "ag-existing")
    _node(r, "ag-caller", status="idle")          # the caller exists before any of it
    assert isinstance(_try_start(r), RuntimeError)
    hit = _assert_hit(r, "limits.max_concurrent", 1, "refused")
    r.tree.set_status("ag-existing", "done", "finished")
    success = _try_start(r)
    assert isinstance(success, dict) and success.get("agent_id"), success
    assert len(_cleared(r, "limits.max_concurrent")) == 1
    assert not any(hit["message"] in a for a in _alerts(r)), "nothing is active any more"

    h.as_subagent(monkeypatch, agent_id="ag-caller", depth=1, can_spawn=True)
    first = asyncio.run(r.wait_for_any(None, 0))
    shown = first.get("limit_notices") or []
    assert [n.get("kind") for n in shown if n.get("key") == "limits.max_concurrent"] == \
        ["limit_hit", "limit_cleared"], first
    second = asyncio.run(r.wait_for_any(None, 0))
    assert not second.get("limit_notices"), second


# ---------------------------------------------------------------------------
# F7 — LN-C4: per-caller cursors do not outlive their callers


def test_d1_f7_cursors_of_ended_callers_are_pruned(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch)
    tag = "ag-caller-pruneprobe"
    callers = [f"{tag}-{i:02d}" for i in range(12)]
    for cid in ("ag-control-pruneprobe", "ag-live-pruneprobe", *callers):
        _node(r, cid, status="idle")
    for cid in callers:
        h.as_subagent(monkeypatch, agent_id=cid, depth=1, can_spawn=True)
        asyncio.run(r.wait_for_any(None, 0))
    for cid in callers:
        r.tree.set_status(cid, "done", "finished")
    # A live caller polls afterwards; whatever housekeeping the runner does, it
    # has had its chance by now.
    h.as_subagent(monkeypatch, agent_id="ag-live-pruneprobe", depth=1, can_spawn=True)
    asyncio.run(r.wait_for_any(None, 0))
    text = r.paths.tree_file.read_text()
    baseline = text.count("ag-control-pruneprobe")      # a node that never waited
    assert baseline >= 1
    leaked = {cid: text.count(cid) for cid in callers if text.count(cid) > baseline}
    assert not leaked, f"tree.json still keeps state for ended callers: {sorted(leaked)}"
    # and a live caller keeps its own cursor: it is not shown the same thing twice
    assert text.count("ag-live-pruneprobe") > baseline


# ---------------------------------------------------------------------------
# F8 — LN-C2: provenance is that of the value in force at launch


@pytest.mark.parametrize("field", ["timeout", "silence_timeout"])
def test_d1_f8_provenance_is_captured_at_launch_not_read_from_current_yaml(tmp_path, monkeypatch, field):
    r, _, agent_file = _project(tmp_path, monkeypatch, agent_lines=[f"{field}: 2"], delay=4,
                                    retry=False)
    launch_file = str(agent_file.resolve())

    async def scenario():
        started = await r.start("worker", "work")
        node_id = started["agent_id"]
        # After launch the operator edits agents.yaml: the value changes and the
        # key moves down the file. The run in flight keeps the value it started with.
        agent_file.write_text("# edited after launch\n# (two comment lines)\n"
                              "agents:\n  worker:\n    provider: fake\n    model: m1\n"
                              f"    {field}: 600\n")
        r.reload(load(r.paths, seed=False))
        await asyncio.wait_for(r.runs[node_id].done.wait(), 20)
        return node_id

    node_id = asyncio.run(scenario())
    hit = _assert_hit(r, f"agents.worker.{field}", 2, "stuck")
    assert hit["node"] == node_id
    assert hit["source"] == {"layer": "agent", "file": launch_file, "line": 5}, hit["source"]
    assert "agents.yaml:5" in hit["message"]
