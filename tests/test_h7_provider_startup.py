"""H7 provider startup contract, exercised through Provider and Runner surfaces."""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.runner import Runner  # noqa: E402


def _exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _provider(name: str, **fields) -> Provider:
    return Provider.from_dict(name, {"bin": name, "spawn": {"args": ["go"]}, **fields})


def _events(r: Runner, kind: str) -> list[dict]:
    if not r.paths.events_file.exists():
        return []
    return [e for line in r.paths.events_file.read_text().splitlines()
            if (e := json.loads(line)).get("kind") == kind]


def _start(r: Runner, **kwargs):
    async def go():
        try:
            result = await r.start("worker", "work", **kwargs)
        except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
            return exc
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _start_agent(r: Runner, monkeypatch, **kwargs):
    import multiagents.server as server
    monkeypatch.setattr(server, "runner", lambda: r)

    async def go():
        result = await server.start_agent("worker", "work", **kwargs)
        run = r.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _known_budget(monkeypatch, *names):
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {
        n: budget_mod.Budget(n, known=True, headroom=1.0) for n in names})


def _runner(tmp_path, monkeypatch, providers, *, limits=None, agent=None):
    _known_budget(monkeypatch, *providers)
    r = h.make_runner(tmp_path / "project", monkeypatch, providers=providers,
                      agents={"worker": agent or AgentSpec.from_dict(
                          "worker", {"provider": next(iter(providers)), "model": "m1"})},
                      project={"limits": {"provider_failure_threshold": 100,
                                          "provider_down_cooldown_seconds": 0.15,
                                          **(limits or {})}})
    for name in providers:
        _exe(r.paths.config / "providers" / f"{name}.sh", "exit 0")
    return r


@pytest.mark.parametrize("route", ["bin", "PATH", "bin_search"])
def test_ps_r1_resolves_each_route_and_reports_search(route, tmp_path, monkeypatch):
    directory = tmp_path / route
    binary = _exe(directory / "testcli")
    monkeypatch.setenv("PATH", str(directory) if route == "PATH" else "")
    p = _provider("p", bin=str(binary) if route == "bin" else "testcli",
                  bin_search=[str(directory)] if route == "bin_search" else [])
    found = p.resolve_bin()
    assert found.path == binary.resolve()
    assert found.launcher == binary
    assert found.via == route
    assert str(directory) in " ".join(map(str, found.searched))


def test_ps_r1_empty_path_skips_nonexecutable_and_rechecks_each_operation(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    _exe(first / "testcli").chmod(0o644)
    binary = _exe(second / "testcli")
    p = _provider("p", bin="testcli", bin_search=[str(first), str(second)])
    assert p.resolve_bin(env={"PATH": ""}).path == binary.resolve()
    binary.unlink()
    assert p.resolve_bin(env={"PATH": ""}).path is None


def test_ps_r1_explicit_missing_path_never_falls_through(tmp_path):
    binary = _exe(tmp_path / "path" / "testcli")
    missing = tmp_path / "absent" / "testcli"
    p = _provider("p", bin=str(missing), bin_search=[str(binary.parent)])
    found = p.resolve_bin(env={"PATH": str(binary.parent)})
    assert found.path is None
    assert str(missing) in " ".join(map(str, found.searched))
    assert str(binary.parent) not in " ".join(map(str, found.searched))


def test_ps_r1_symlink_launcher_is_distinct_from_resolved_path(tmp_path):
    target = _exe(tmp_path / "versions" / "v1" / "testcli")
    launcher = tmp_path / "bin" / "testcli"
    launcher.parent.mkdir()
    launcher.symlink_to(target)
    found = _provider("p", bin="testcli").resolve_bin(env={"PATH": str(launcher.parent)})
    assert found.launcher == launcher
    assert found.path == target.resolve()
    assert found.via == "PATH"


def test_ps_r1_rejects_relative_bin_search_at_config_load(tmp_path):
    from multiagents.providers import load_providers
    with pytest.raises(ValueError, match="bin_search"):
        load_providers({"testcli": {"bin": "testcli", "bin_search": ["relative/bin"]}})


def test_ps_r2_native_launch_uses_bin_search_without_binary_on_path(tmp_path, monkeypatch):
    probe = tmp_path / "called"
    directory = tmp_path / "extra"
    _exe(directory / "testcli", f"touch '{probe}'\nprintf '%s\\n' '{{\"type\":\"text\",\"text\":\"ok\"}}'")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    provider = {"bin": "testcli", "bin_search": [str(directory)],
                "spawn": {"args": ["go"]}, "stream": {"format": "ndjson", "rules": [
                    {"match": {"type": "text"}, "as": "text", "fields": {"text": "text"}}]}}
    r = _runner(tmp_path, monkeypatch, {"testcli": provider})
    result = _start(r)
    assert isinstance(result, dict) and result.get("provider") == "testcli", result
    assert probe.exists()


def test_ps_r2_budget_script_runs_without_binary_and_gets_error(tmp_path):
    from multiagents import scripts
    from multiagents.executor.local import LocalExecutor
    script_dir = tmp_path / "providers"
    script = _exe(script_dir / "p.sh", 'test "$1" = budget || exit 21\nprintf \'%s|%s\' "$MULTIAGENTS_BIN" "$MULTIAGENTS_BIN_ERROR"')
    p = _provider("p", bin="unfindable-h7-bin", script=script.name)
    code, out, err = scripts.run_action("p", p, LocalExecutor(), "budget", tmp_path, None)
    assert code == 0, err
    resolved, message = out.split("|", 1)
    assert resolved == ""
    assert "p" in message and "bin_search" in message


def test_ps_r3_start_error_lists_search_places_and_fix(tmp_path, monkeypatch):
    missing_dir = tmp_path / "extra"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = {"bin": "unfindable-h7-bin", "bin_search": [str(missing_dir)],
         "spawn": {"args": ["go"]}}
    r = _runner(tmp_path, monkeypatch, {"p": p})
    result = _start_agent(r, monkeypatch)
    message = str(result.get("error", ""))
    assert all(s in message for s in ("p", str(missing_dir), "bin:", "bin_search:", "providers.yaml")), message


def _stream_provider(tmp_path, name, events):
    shipped = yaml.safe_load((Path(__file__).parents[1] / "src/multiagents/defaults/providers.yaml").read_text())
    stream = shipped["providers"][name]["stream"]
    config = h.fake_cli(tmp_path, name, events=events, exit_code=1, stderr="startup exploded\n")
    config["stream"] = stream
    return config


@pytest.mark.parametrize("name,init,text_event", [
    ("claude", {"type": "system", "subtype": "init", "session_id": "s"},
     {"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "hello"}]}}),
    ("codex", {"kind": "step", "type": "thread.started", "session_id": "s"},
     {"kind": "text", "text": "hello"}),
])
@pytest.mark.parametrize("has_text", [False, True])
def test_ps_r4_init_is_not_progress_but_assistant_text_is(tmp_path, monkeypatch, name, init, text_event, has_text):
    events = [init, text_event] if has_text else [init]
    provider = _stream_provider(tmp_path, name, events)
    r = _runner(tmp_path, monkeypatch, {name: provider},
                limits={"startup_failure_threshold": 1})
    _start(r)
    down = _events(r, "startup_down")
    assert bool(down) is not has_text, down
    if down:
        assert down[-1].get("provider") == name
        assert down[-1].get("count", down[-1].get("failures")) == 1
        assert "startup exploded" in str(down[-1])


def test_ps_r4_two_failures_trip_and_other_provider_success_does_not_reset(tmp_path, monkeypatch):
    primary = h.fake_cli(tmp_path, "primary", exit_code=1, stderr="first failure")
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "ok"}])
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1",
                                            "models": {"sibling": "sibling-m1"}})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling}, agent=agent,
                limits={"startup_failure_threshold": 2})
    _start(r, model="m1")
    assert not _events(r, "startup_down")
    assert _start(r, model="sibling-m1").get("provider") == "sibling"
    _start(r, model="m1")
    down = _events(r, "startup_down")
    assert len(down) == 1 and down[0].get("provider") == "primary", down
    assert down[0].get("count", down[0].get("failures")) == 2


def test_ps_r6_pinned_down_refuses_without_sibling_run(tmp_path, monkeypatch):
    primary = h.fake_cli(tmp_path, "primary", exit_code=1, stderr="startup exploded")
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "ok"}])
    marker = tmp_path / "sibling-started"
    sibling_bin = Path(sibling["bin"])
    sibling_bin.write_text(sibling_bin.read_text().replace("import json, sys, time", f"import json, sys, time\nopen({str(marker)!r}, 'w').close()"))
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1",
                                            "models": {"sibling": "other-model"}})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling}, agent=agent,
                limits={"startup_failure_threshold": 1,
                        "provider_down_cooldown_seconds": 30})
    _start(r)
    result = _start_agent(r, monkeypatch, model="m1")
    assert not marker.exists()
    assert isinstance(result, dict) and result.get("reason") and not result.get("agent_id"), result
    assert "omitting" in str(result).lower() and "model" in str(result).lower()
    unpinned = _start_agent(r, monkeypatch)
    assert isinstance(unpinned, dict) and unpinned.get("provider") == "sibling", unpinned
    assert marker.exists()


def _trip_primary(tmp_path, monkeypatch, *, cooldown=0.15):
    primary = h.fake_cli(tmp_path, "primary", exit_code=1, stderr="startup exploded")
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "ok"}])
    sibling["family"] = "primary"
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1"})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling},
                agent=agent, limits={"startup_failure_threshold": 1,
                                     "provider_down_cooldown_seconds": cooldown})
    _start(r, model="m1")
    assert _events(r, "startup_down")
    return r, primary


def test_ps_r5_restart_and_tree_forgery_cannot_clear_host_mark(tmp_path, monkeypatch):
    r, _ = _trip_primary(tmp_path, monkeypatch, cooldown=30)
    raw = json.loads(r.paths.tree_file.read_text())
    raw.pop("startup_down", None)
    raw.pop("startup_failures", None)
    raw["provider_health"] = {}
    raw["cooldowns"] = {}
    r.paths.tree_file.write_text(json.dumps(raw))
    fresh = Runner(r.paths, r.config)
    result = _start(fresh, model="m1")
    assert isinstance(result, dict) and result.get("reason") and not result.get("agent_id"), result
    assert "primary" in str(result)


def test_ps_r5_half_open_has_one_probe_and_progress_recovers(tmp_path, monkeypatch):
    r, primary = _trip_primary(tmp_path, monkeypatch)
    import time
    time.sleep(0.2)
    marker = tmp_path / "probes"
    h.fake_cli(tmp_path, "primary", events=[{"type": "text", "text": "recovered"}],
               delay=0.4)
    binary = Path(primary["bin"])
    binary.write_text(binary.read_text().replace(
        "import json, sys, time", f"import json, sys, time\nwith open({str(marker)!r}, 'a') as f: f.write('probe\\n')"))
    other = Runner(r.paths, r.config)

    async def both():
        async def attempt(runner):
            try:
                return await runner.start("worker", "work", model="m1")
            except (RuntimeError, PermissionError, ValueError, FileNotFoundError) as exc:
                return exc
        results = await asyncio.gather(attempt(r), attempt(other))
        for runner, result in zip((r, other), results):
            if isinstance(result, dict) and result.get("agent_id"):
                await asyncio.wait_for(runner.runs[result["agent_id"]].done.wait(), 15)
        return results

    results = asyncio.run(both())
    assert marker.read_text().splitlines() == ["probe"]
    assert sum(isinstance(x, dict) and bool(x.get("agent_id")) for x in results) == 1, results
    assert len(_events(r, "provider_recovered")) == 1
    assert _start(Runner(r.paths, r.config), model="m1").get("provider") == "primary"


def test_ps_r5_failed_probe_remains_down_for_another_cooldown(tmp_path, monkeypatch):
    r, _ = _trip_primary(tmp_path, monkeypatch)
    import time
    time.sleep(0.2)
    probe = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(probe, dict) and probe.get("provider") == "primary", probe
    refused = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(refused, dict) and refused.get("reason") and not refused.get("agent_id"), refused
    assert not _events(r, "provider_recovered")


def test_ps_r6_pinned_healthy_stays_on_requested_family_account(tmp_path, monkeypatch):
    primary = h.fake_cli(tmp_path, "primary", events=[{"type": "text", "text": "primary"}])
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "sibling"}])
    sibling["family"] = "primary"
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1"})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling}, agent=agent)
    result = _start_agent(r, monkeypatch, model="m1")
    assert isinstance(result, dict) and result.get("provider") == "primary", result


def test_ps_r2_auth_check_receives_resolved_binary_from_bin_search(tmp_path, monkeypatch):
    from multiagents import scripts
    from multiagents.executor.local import LocalExecutor
    directory = tmp_path / "extra"
    binary = _exe(directory / "testcli")
    _exe(tmp_path / "providers" / "p.sh", 'test "$1" = check || exit 21\nprintf "%s" "$MULTIAGENTS_BIN"')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = _provider("p", bin="testcli", bin_search=[str(directory)], script="p.sh")
    code, out, err = scripts.run_action("p", p, LocalExecutor(), "check", tmp_path)
    assert code == 0, err
    assert out == str(binary)


def test_ps_r2a_bin_is_the_symlink_launcher_while_identity_is_the_target(tmp_path, monkeypatch):
    from multiagents import scripts
    from multiagents.executor.local import LocalExecutor
    target = _exe(tmp_path / "versions" / "v1" / "testcli")
    directory = tmp_path / "extra"
    launcher = directory / "testcli"
    directory.mkdir()
    launcher.symlink_to(target)
    _exe(tmp_path / "providers" / "p.sh", 'test "$1" = check || exit 21\nprintf "%s" "$MULTIAGENTS_BIN"')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = _provider("p", bin="testcli", bin_search=[str(directory)], script="p.sh")
    assert p.resolve_bin().path == target.resolve()
    code, out, err = scripts.run_action("p", p, LocalExecutor(), "check", tmp_path)
    assert code == 0, err
    assert out == str(launcher)
    assert out != str(target.resolve())


def test_ps_r2b_provider_env_overrides_resolved_bin(tmp_path, monkeypatch):
    from multiagents import scripts
    from multiagents.executor.local import LocalExecutor
    directory = tmp_path / "extra"
    _exe(directory / "testcli")
    _exe(tmp_path / "providers" / "p.sh", 'test "$1" = check || exit 21\nprintf "%s" "$MULTIAGENTS_BIN"')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = _provider("p", bin="testcli", bin_search=[str(directory)], script="p.sh",
                  env={"MULTIAGENTS_BIN": "/custom/x"})
    code, out, err = scripts.run_action("p", p, LocalExecutor(), "check", tmp_path)
    assert code == 0, err
    assert out == "/custom/x"


def test_ps_r2_refresh_models_rewrites_command_to_resolved_binary(tmp_path, monkeypatch):
    from multiagents.models import refresh_models
    directory = tmp_path / "extra"
    binary = _exe(directory / "testcli", 'printf "model-one\\n"')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = _provider("p", bin="testcli", bin_search=[str(directory)],
                  models_cmd=["testcli", "models"])
    target = tmp_path / "models.yaml"
    result = refresh_models({"p": p}, target)
    assert result["counts"]["p"] == 1, result
    assert yaml.safe_load(target.read_text())["models"]["p"][0]["id"] == "model-one"


@pytest.mark.parametrize("found", [False, True])
def test_ps_r3_doctor_shows_resolution_or_search_fix(tmp_path, monkeypatch, capsys, found):
    import argparse
    import multiagents.cli as cli
    root = h.make_git_repo(tmp_path / "project")
    paths = ProjectPaths(root)
    paths.ensure()
    directory = tmp_path / "extra"
    if found:
        binary = _exe(directory / "testcli")
    provider = {"bin": "testcli", "bin_search": [str(directory)],
                "spawn": {"args": ["go"]}}
    config = h.make_config(providers={"p": provider})
    monkeypatch.setattr(cli, "load_config", lambda _paths: config)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    cli.cmd_doctor(argparse.Namespace(path=str(root), clear=None, force=False))
    output = capsys.readouterr().out
    assert "p" in output
    if found:
        assert str(binary.resolve()) in output and "bin_search" in output
    else:
        assert str(directory) in output and "bin:" in output
        assert "bin_search:" in output and "providers.yaml" in output


def test_ps_r5_empty_probe_keeps_mark_and_releases_claim(tmp_path, monkeypatch):
    r, primary = _trip_primary(tmp_path, monkeypatch, cooldown=0.5)
    import time
    time.sleep(0.55)
    h.fake_cli(tmp_path, "primary", events=[], exit_code=0)
    first = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(first, dict) and first.get("provider") == "primary", first
    assert not _events(r, "provider_recovered")
    blocked = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(blocked, dict) and blocked.get("reason") and not blocked.get("agent_id"), blocked
    time.sleep(0.55)
    again = _start(Runner(r.paths, r.config), model="m1")
    assert isinstance(again, dict) and again.get("provider") == "primary", again


def test_ps_r1_shipped_opencode_searches_its_installer_directory():
    shipped = yaml.safe_load((Path(__file__).parents[1] / "src/multiagents/defaults/providers.yaml").read_text())
    assert "~/.opencode/bin" in shipped["providers"]["opencode"]["bin_search"]


def test_ps_r1_expands_tilde_in_bin_search(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = _exe(tmp_path / ".local" / "bin" / "testcli")
    p = _provider("p", bin="testcli", bin_search=["~/.local/bin"])
    found = p.resolve_bin(env={"PATH": ""})
    assert found.path == binary.resolve()
    assert found.via == "bin_search"


def test_ps_r1_rejects_relative_explicit_bin():
    with pytest.raises(ValueError, match="bin"):
        _provider("p", bin="./relative/testcli").resolve_bin(env={"PATH": ""})


@pytest.mark.parametrize("blocked", ["disabled", "missing_binary"])
def test_ps_r6_pinned_unavailable_provider_refuses_without_family_fallback(
        tmp_path, monkeypatch, blocked):
    marker = tmp_path / "sibling-called"
    sibling = h.fake_cli(tmp_path, "sibling", events=[{"type": "text", "text": "ok"}])
    sibling["family"] = "primary"
    script = Path(sibling["bin"])
    script.write_text(script.read_text().replace(
        "import json, sys, time", f"import json, sys, time\nopen({str(marker)!r}, 'w').close()"))
    primary = h.fake_cli(tmp_path, "primary", events=[{"type": "text", "text": "ok"}])
    if blocked == "disabled":
        primary["enabled"] = False
    else:
        primary["bin"] = str(tmp_path / "absent-cli")
    agent = AgentSpec.from_dict("worker", {"provider": "primary", "model": "m1"})
    r = _runner(tmp_path, monkeypatch, {"primary": primary, "sibling": sibling}, agent=agent)
    result = _start_agent(r, monkeypatch, model="m1")
    assert result.get("reason"), result
    assert not result.get("agent_id") and not marker.exists(), result
    assert "omitting" in str(result).lower() and "model" in str(result).lower()
