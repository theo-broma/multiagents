"""D1 limit notices through a loaded project, Runner, tree events and monitor."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents.config import load  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.monitor.snapshot import snapshot  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node  # noqa: E402


@pytest.fixture(autouse=True)
def _known_provider_headroom(monkeypatch):
    # Quota is an external provider reading; these tests concern local limits.
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "fake": budget_mod.Budget("fake", known=True, headroom=1.0)})


def _project(tmp_path, monkeypatch, *, project_lines=(), agent_lines=(), delay=0):
    root = h.make_git_repo(tmp_path / "project")
    paths = ProjectPaths(root)
    paths.ensure()
    project_file = paths.config / "project.yaml"
    project_file.write_text("team: ''\nlimits:" + ("\n" if project_lines else " {}\n") +
                            "".join(f"  {line}\n" for line in project_lines))
    agent_file = paths.config / "agents.yaml"
    agent_file.write_text("agents:\n  worker:\n    provider: fake\n    model: m1\n" +
                          "".join(f"    {line}\n" for line in agent_lines))
    provider = h.fake_cli(tmp_path, "fake", delay=delay)
    import yaml
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fake": provider}}))
    h.as_root(monkeypatch)
    return Runner(paths, load(paths, seed=False)), project_file, agent_file


def _events(r, kind):
    if not r.paths.events_file.exists():
        return []
    return [event for line in r.paths.events_file.read_text().splitlines()
            if (event := json.loads(line)).get("kind") == kind]


def _try_start(r, **kwargs):
    async def go():
        try:
            result = await r.start("worker", "work", **kwargs)
        except (RuntimeError, PermissionError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _assert_hit(r, key, value, effect, source=None):
    hits = [e for e in _events(r, "limit_hit") if e.get("key") == key]
    assert len(hits) == 1, (key, _events(r, "limit_hit"))
    hit = hits[0]
    assert hit["value"] == value
    assert hit["effect"] == effect
    assert hit.get("scope")
    assert isinstance(hit.get("message"), str) and key in hit["message"]
    assert str(value) in hit["message"]
    assert "source" in hit
    if source is not None:
        assert hit["source"] == source
    return hit


@pytest.mark.parametrize("key,value,setup", [
    ("limits.max_concurrent", 1, "concurrent"),
    ("limits.max_depth", 0, "depth"),
    ("limits.max_children", 1, "children"),
    ("limits.budget_tokens", 1, "budget"),
])
def test_ln_c1_ln_c6_refusal_rows(tmp_path, monkeypatch, key, value, setup):
    r, project_file, _ = _project(tmp_path, monkeypatch, project_lines=[f"{key.split('.')[-1]}: {value}"])
    if setup == "concurrent":
        r.tree.add(Node(id="ag-existing", agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="running"))
    elif setup == "children":
        r.tree.add(Node(id="ag-parent", agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="running"))
        r.tree.add(Node(id="ag-sibling", agent="worker", provider="fake", model="m1",
                        parent="ag-parent", depth=2, status="running"))
        h.as_subagent(monkeypatch, agent_id="ag-parent", depth=1, can_spawn=True)
    elif setup == "budget":
        r.tree.add(Node(id="ag-spent", agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="done", usage={"total_tokens": 1}))
    result = _try_start(r)
    assert isinstance(result, (RuntimeError, PermissionError)), result
    source = {"layer": "project", "file": str(project_file.resolve()), "line": 3}
    hit = _assert_hit(r, key, value, "refused", source)
    assert hit["message"] in str(result)


def test_ln_c1_ln_c6_budget_tag_ceiling_refusal(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch)
    first = _try_start(r, budget_tag="slice", budget_tokens=1)
    assert isinstance(first, dict) and first.get("agent_id"), first
    r.tree.add(Node(id="ag-spent", agent="worker", provider="fake", model="m1",
                    parent=None, depth=1, status="done", budget_tag="slice",
                    usage={"total_tokens": 1}))
    result = _try_start(r, budget_tag="slice", budget_tokens=99)
    assert isinstance(result, RuntimeError), result
    hit = _assert_hit(r, "budget_tag.slice", 1, "refused")
    assert hit["scope"] == "slice"
    assert hit["source"]["layer"] == "call"
    assert hit["source"]["tool"] == "start_agent"
    assert hit["message"] in str(result)


def test_ln_c2_builtin_source_points_to_shipped_default_and_project_override(tmp_path, monkeypatch):
    r, project_file, _ = _project(tmp_path, monkeypatch)
    for i in range(4):
        r.tree.add(Node(id=f"ag-{i}", agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="running"))
    assert isinstance(_try_start(r), RuntimeError)
    hit = _assert_hit(r, "limits.max_concurrent", 4, "refused")
    source = hit["source"]
    assert source["layer"] == "builtin"
    assert Path(source["file"]).is_absolute()
    assert Path(source["file"]).read_text().splitlines()[source["line"] - 1].lstrip().startswith("max_concurrent:")
    assert source["override_file"] == str(project_file.resolve())
    assert source["override_key"] == "limits.max_concurrent"


@pytest.mark.parametrize("field,key", [("timeout", "timeout"), ("silence_timeout", "silence_timeout")])
def test_ln_c1_ln_c2_ln_c6_agent_watchdog_source(tmp_path, monkeypatch, field, key):
    r, _, agent_file = _project(tmp_path, monkeypatch, agent_lines=[f"{field}: 1"], delay=7)
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("agent_id"), result
    hit = _assert_hit(r, f"agents.worker.{key}", 1, "stuck")
    assert hit["node"] == result["agent_id"]
    assert hit["source"] == {"layer": "agent", "file": str(agent_file.resolve()), "line": 5}


def test_ln_c2_call_timeout_overrides_agent_and_project_source(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["default_timeout: 10"],
                        agent_lines=["timeout: 9"], delay=7)
    result = _try_start(r, timeout=1)
    assert isinstance(result, dict) and result.get("agent_id"), result
    hit = _assert_hit(r, "limits.default_timeout", 1, "stuck")
    assert hit["source"] == {"layer": "call", "tool": "start_agent", "argument": "timeout",
                              "override_key": "limits.default_timeout"}


def test_ln_c1_under_limit_emits_no_notice(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("agent_id"), result
    assert not _events(r, "limit_hit")


def test_ln_c3_monitor_alert_and_wait_cursors(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    r.tree.add(Node(id="ag-existing", agent="worker", provider="fake", model="m1",
                    parent=None, depth=1, status="running"))
    assert isinstance(_try_start(r), RuntimeError)
    hit = _assert_hit(r, "limits.max_concurrent", 1, "refused")
    shown = snapshot(r.paths, r.config, with_scripts=False)
    assert any(hit["message"] in str(alert) for alert in shown["alerts"]), shown["alerts"]
    first = asyncio.run(r.wait_for_any(None, 0))
    second = asyncio.run(r.wait_for_any(None, 0))
    assert hit["message"] in str(first.get("limit_notices")), first
    assert hit["message"] not in str(second.get("limit_notices")), second


def test_ln_c4_repeated_refusals_across_runners_clear_once_on_success(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    r.tree.add(Node(id="ag-existing", agent="worker", provider="fake", model="m1",
                    parent=None, depth=1, status="running"))
    for i in range(10):
        if i == 5:
            r = Runner(r.paths, load(r.paths, seed=False))
        assert isinstance(_try_start(r), RuntimeError)
    hit = _assert_hit(r, "limits.max_concurrent", 1, "refused")
    assert any(hit["message"] in str(a) for a in snapshot(r.paths, r.config, with_scripts=False)["alerts"])
    r.tree.set_status("ag-existing", "done", "finished")
    success = _try_start(r)
    assert isinstance(success, dict) and success.get("agent_id"), success
    clears = [e for e in _events(r, "limit_cleared") if e.get("key") == "limits.max_concurrent"]
    assert len(clears) == 1, clears
    assert clears[0]["scope"] == hit["scope"] and clears[0]["count"] == 10
    assert not any(hit["message"] in str(a) for a in snapshot(r.paths, r.config, with_scripts=False)["alerts"])


@pytest.mark.parametrize("key,effect,lines", [
    ("limits.max_steps", "stuck", [{"type": "step"}, {"type": "step"}]),
    ("limits.doom_loop_repeats", "stuck", [
        {"type": "tool", "name": "view_file", "input": {"path": "same.txt"}},
        {"type": "tool", "name": "view_file", "input": {"path": "same.txt"}},
    ]),
])
def test_ln_c1_ln_c6_supervisor_rows(tmp_path, monkeypatch, key, effect, lines):
    r, project_file, _ = _project(tmp_path, monkeypatch,
                                  project_lines=[f"{key.split('.')[-1]}: 1"])
    provider = h.fake_cli(tmp_path, "stream", events=lines)
    provider["stream"]["rules"] = [
        {"match": {"type": "step"}, "as": "step", "fields": {}},
        {"match": {"type": "tool"}, "as": "tool", "fields": {"name": "name", "args": "input"}},
        *provider["stream"]["rules"],
    ]
    import yaml
    (r.paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fake": provider}}))
    r = Runner(r.paths, load(r.paths, seed=False))
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("agent_id"), result
    hit = _assert_hit(r, key, 1, effect,
                      {"layer": "project", "file": str(project_file.resolve()), "line": 3})
    assert hit["node"] == result["agent_id"]
    assert any(e.get("kind") == "stuck" and e.get("agent") == result["agent_id"]
               for e in (json.loads(line) for line in r.paths.events_file.read_text().splitlines()))


def test_ln_c3_run_terminal_prints_notice_while_provider_active(tmp_path, monkeypatch):
    import os
    import subprocess
    import time
    import yaml

    r, _, agent_file = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    # The shipped `orchestrator` entry is disabled so `captain`, on the fake
    # provider, is the only launchable orchestrator.
    agent_file.write_text(agent_file.read_text() +
                          "  orchestrator:\n    disabled: true\n"
                          "  captain:\n    provider: fake\n    model: m1\n    launch: true\n    role: orchestrator\n")
    launched = tmp_path / "fake-provider-launched"
    provider_script = r.paths.config / "providers" / "fake.py"
    provider_script.parent.mkdir()
    provider_script.write_text(
        f"#!{sys.executable}\nimport pathlib, sys, time\n"
        "if sys.argv[1] == 'check': sys.exit(0)\n"
        f"if sys.argv[1] == 'launch': pathlib.Path({str(launched)!r}).touch(); "
        "print('FAKE_PROVIDER_ACTIVE', flush=True); time.sleep(4); sys.exit(0)\n"
        "sys.exit(64)\n")
    provider_script.chmod(0o755)
    providers = yaml.safe_load((r.paths.config / "providers.yaml").read_text())
    providers["providers"]["fake"]["script"] = "fake.py"
    (r.paths.config / "providers.yaml").write_text(yaml.safe_dump(providers))
    r = Runner(r.paths, load(r.paths, seed=False))
    # Shadow every shipped provider CLI so a misrouted launch cannot reach a
    # real one on this machine; a shim records that it was invoked.
    shims = tmp_path / "provider-shims"
    shims.mkdir()
    real_cli_hit = tmp_path / "real-provider-cli-invoked"
    for name in ("claude", "codex", "agy", "opencode"):
        shim = shims / name
        shim.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {str(real_cli_hit)!r}\nexit 97\n")
        shim.chmod(0o755)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONUNBUFFERED="1",
               PATH=f"{shims}{os.pathsep}{os.environ.get('PATH', '')}")
    process = subprocess.Popen([sys.executable, "-m", "multiagents.cli", "--path", str(r.paths.root),
                                "run", "--no-supervise"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
    try:
        # The fake provider is deliberately still alive when the refusal occurs.
        time.sleep(1)
        assert process.poll() is None, "run exited before the fake provider could receive the notice"
        r.tree.add(Node(id="ag-existing", agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="running"))
        assert isinstance(_try_start(r), RuntimeError)
        hit = _assert_hit(r, "limits.max_concurrent", 1, "refused")
        output, _ = process.communicate(timeout=8)
        assert hit["message"] in output and "limit_hit" in output, output
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
    assert not real_cli_hit.exists(), real_cli_hit.read_text()
    assert launched.exists(), "the fake provider was never launched as the orchestrator"


def test_ln_c2_effective_limits_carry_precise_provenance(tmp_path, monkeypatch):
    r, project_file, agent_file = _project(tmp_path, monkeypatch,
        project_lines=["default_timeout: 4", "max_children: 1"],
        agent_lines=["silence_timeout: 6"])
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("agent_id"), result
    # Amended LN-C2: `source` stays LM-R2's layer string; the detail is beside it.
    assert result.get("effective_limits") == {
        "timeout": {"value": 4, "source": "project", "source_detail": {"layer": "project",
            "file": str(project_file.resolve()), "line": 3}},
        "max_children": {"value": 1, "source": "project", "source_detail": {"layer": "project",
            "file": str(project_file.resolve()), "line": 4}},
        "silence_timeout": {"value": 6, "source": "agent", "source_detail": {"layer": "agent",
            "file": str(agent_file.resolve()), "line": 5}},
    }


def test_ln_c1_ln_c6_reserve_headroom_defers_only_when_reserve_causes_it(tmp_path, monkeypatch):
    import yaml
    r, project_file, _ = _project(tmp_path, monkeypatch)
    project_file.write_text("team: ''\nlimits: {}\nbudget:\n  reserve_headroom: 0.2\n  reserve: true\n")
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "fake": budget_mod.Budget("fake", known=True, headroom=0.1)})
    r = Runner(r.paths, load(r.paths, seed=False))
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("deferred"), result
    hit = _assert_hit(r, "budget.reserve_headroom", 0.2, "deferred")
    assert hit["source"] == {"layer": "project", "file": str(project_file.resolve()), "line": 4}
    assert any(e.get("kind") == "deferred" for line in r.paths.events_file.read_text().splitlines()
               if (e := json.loads(line)))


@pytest.mark.parametrize("key,field", [
    ("limits.default_timeout", "default_timeout"),
    ("limits.silence_timeout", "silence_timeout"),
])
def test_ln_c1_ln_c6_project_watchdog_rows(tmp_path, monkeypatch, key, field):
    r, project_file, _ = _project(tmp_path, monkeypatch,
                                  project_lines=[f"{field}: 1"], delay=7)
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("agent_id"), result
    hit = _assert_hit(r, key, 1, "stuck",
                      {"layer": "project", "file": str(project_file.resolve()), "line": 3})
    assert hit["node"] == result["agent_id"]


@pytest.mark.parametrize("key,limit,plans", [
    ("limits.commit_fix_attempts", 1, "exhaust"),
    ("limits.commit_fix_timeout", 1, "timeout"),
])
def test_ln_c1_ln_c6_commit_fix_rows(tmp_path, monkeypatch, key, limit, plans):
    import yaml
    from test_commit_identity_r5 import FIRST, NO_FIX, fake_provider, install_hook, run_to_end
    if plans == "exhaust":
        provider, _ = fake_provider(tmp_path, [FIRST, NO_FIX])
        settings = ["commit_fix_attempts: 1", "commit_fix_timeout: 5"]
    else:
        from test_commit_identity_r5 import text
        hung = [["gate", "never"], text("late fix"), ["exit", 0]]
        provider, _ = fake_provider(tmp_path, [FIRST, hung])
        settings = ["commit_fix_attempts: 1", "commit_fix_timeout: 1"]
    root = h.make_git_repo(tmp_path / "project")
    paths = ProjectPaths(root)
    paths.ensure()
    project_file = paths.config / "project.yaml"
    project_file.write_text("team: ''\nlimits:\n" + "".join(f"  {s}\n" for s in settings))
    (paths.config / "agents.yaml").write_text(
        "agents:\n  worker:\n    provider: fake\n    model: m1\n    silence_timeout: 120\n")
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fake": provider}}))
    install_hook(root)
    h.as_root(monkeypatch)
    r = Runner(paths, load(paths, seed=False))
    agent_id = run_to_end(r, timeout=20)
    assert agent_id
    assert any(e.get("agent") == agent_id for e in _events(r, "commit_fix_attempt")), \
        "fixture did not reach the commit-fix limit"
    hit = _assert_hit(r, key, limit, "stopped",
                      {"layer": "project", "file": str(project_file.resolve()),
                       "line": 3 if plans == "exhaust" else 4})
    assert hit["node"] == agent_id


def test_ln_c1_ln_c6_wind_down_defers_when_window_would_end_first(tmp_path, monkeypatch):
    import time
    import yaml
    from multiagents import tree as tree_mod
    r, project_file, _ = _project(tmp_path, monkeypatch)
    project_file.write_text(
        "team: ''\nlimits:\n  wind_down_seconds: 60\n"
        "budget:\n  burn_min_span_seconds: 0\n  burn_min_samples: 2\n")
    # Two public headroom observations thirty seconds apart. Advancing the
    # clock avoids a real thirty-second wait; no notice machinery is mocked.
    clock = [time.time() - 30]
    monkeypatch.setattr(tree_mod, "now", lambda: clock[0])
    r.tree.note_headroom("fake", 0.2)
    clock[0] += 30
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        "fake": budget_mod.Budget("fake", known=True, headroom=0.1)})
    r = Runner(r.paths, load(r.paths, seed=False))
    result = _try_start(r)
    assert isinstance(result, dict) and result.get("deferred"), result
    hit = _assert_hit(r, "limits.wind_down_seconds", 60, "deferred")
    assert hit["source"] == {"layer": "project", "file": str(project_file.resolve()), "line": 3}


def test_ln_c4_wait_notice_cursor_is_independent_for_each_caller(tmp_path, monkeypatch):
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    r.tree.add(Node(id="ag-existing", agent="worker", provider="fake", model="m1",
                    parent=None, depth=1, status="running"))
    assert isinstance(_try_start(r), RuntimeError)
    hit = _assert_hit(r, "limits.max_concurrent", 1, "refused")
    for caller in ("ag-caller-a", "ag-caller-b"):
        r.tree.add(Node(id=caller, agent="worker", provider="fake", model="m1",
                        parent=None, depth=1, status="idle"))
        h.as_subagent(monkeypatch, agent_id=caller, depth=1, can_spawn=True)
        first = asyncio.run(r.wait_for_any(None, 0))
        second = asyncio.run(r.wait_for_any(None, 0))
        assert hit["message"] in str(first.get("limit_notices")), first
        assert hit["message"] not in str(second.get("limit_notices")), second


@pytest.mark.parametrize("counter_readable,counter_increases,sibling,effect,key", [
    (True, True, False, "killed", "executor.docker.memory"),
    (True, True, True, "kill_uncertain", "process.sigkill"),
    (True, False, False, "kill_uncertain", "process.sigkill"),
    (False, False, False, "kill_uncertain", "process.sigkill"),
], ids=["sole-oom", "concurrent-oom", "unchanged-counter", "unreadable-counter"])
def test_ln_c5_ln_c6_docker_sigkill_attribution(
        tmp_path, monkeypatch, counter_readable, counter_increases, sibling, effect, key):
    """An exec exit 137 is attributed only with an increased counter and sole occupancy."""
    from multiagents import gitops, runner as runner_mod
    from multiagents.executor.docker import DockerExecutor
    from multiagents.executor.local import LocalExecutor
    import yaml

    r, project_file, _ = _project(tmp_path, monkeypatch)
    project_file.write_text(
        "team: ''\nlimits:\n  retry_silent_failure_under_seconds: 0\n"
        "executor:\n  kind: docker\n  docker:\n    memory: 64m\n")
    provider = h.fake_cli(tmp_path, "kill", events=[{"type": "text", "text": "before kill"}],
                          exit_code=137, delay=2)
    (r.paths.config / "providers.yaml").write_text(yaml.safe_dump({
        "providers": {"fake": provider}}))
    r = Runner(r.paths, load(r.paths, seed=False))

    class FakeDocker(DockerExecutor):
        # The executor boundary runs a real fake provider CLI under the local
        # launch wrapper. Only docker exec is replaced; Runner owns both runs.
        async def start(self, argv, cwd, env, **kwargs):
            return await LocalExecutor.start(self, argv, cwd, env, **kwargs)

        async def _start_wrapped(self, argv, cwd, env, run_dir, deadline, pid_file):
            return await LocalExecutor._start_wrapped(self, argv, cwd, env,
                                                      run_dir, deadline, pid_file)

        def preflight(self):
            return []

        def git(self, agent_id):
            return gitops.HOST

    fake = FakeDocker({}, r.paths, r.providers)
    monkeypatch.setattr(runner_mod, "get_executor", lambda *a, **k: fake)
    counter = [10 if counter_readable else None]
    # The contract's single cgroup-reader seam. None models an unreadable
    # memory.events file; no docker inspect result is supplied.
    monkeypatch.setattr(DockerExecutor, "oom_kill_count", lambda self: counter[0],
                        raising=False)

    async def scenario():
        first = await r.start("worker", "first")
        first_id = first["agent_id"]
        if sibling:
            second = await r.start("worker", "overlapping sibling")
            second_id = second["agent_id"]
            assert r.tree.get(first_id).status == "running"
            assert r.tree.get(second_id).status == "running"
        else:
            second_id = None
        if counter_increases:
            counter[0] = 11
        await asyncio.wait_for(r.runs[first_id].done.wait(), 10)
        if second_id:
            await asyncio.wait_for(r.runs[second_id].done.wait(), 10)
        return first_id

    killed_id = asyncio.run(scenario())
    result = json.loads((r.paths.run_dir(killed_id) / "result.json").read_text())
    assert result["exit_code"] == 137, result
    hits = [e for e in _events(r, "limit_hit") if e.get("node") == killed_id]
    assert len(hits) == 1, hits
    hit = hits[0]
    assert hit["key"] == key and hit["effect"] == effect, hit
    assert hit["scope"] == killed_id
    assert key in hit["message"] and "SIGKILL" in hit["message"].upper()
    if key == "executor.docker.memory":
        assert hit["value"] == "64m"
        assert hit["source"] == {"layer": "project", "file": str(project_file.resolve()),
                                 "line": 7}
    else:
        assert hit["source"] is None
        assert not [e for e in hits if e.get("key") == "executor.docker.memory"]
