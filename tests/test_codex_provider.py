"""Codex provider contract: layout, agent runs, launch/compact (CX-C7, C10, C13).

Black box. The adapter is executed, never imported; the native CLI is the fake
in `support/codex_harness.py`. Started from the user's proposal tests
(context/codex-proposal/tests/test_codex.py), adapted to the shipped paths
and to the contract where it differs: no `home_links`, no interactive launch,
the adapter picks the profile, and `MULTIAGENTS_BIN` names the native CLI.

The proposal's `sessions()` and interactive-launch tests are not carried over:
CX-C13 withdraws `launch` for this phase, and `sessions()` was internal to it.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402
from multiagents.providers import RAW, RESULT, TEXT, TOOL           # noqa: E402
from multiagents.supervisor import looks_like_quota_failure         # noqa: E402
from multiagents.tree import token_count                            # noqa: E402

SID = "0199a213-81c0-7800-8aa1-bbab2a035a53"

HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed",
     "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 10}},
]


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodex(tmp_path)


def run_agent(tmp_path, fake, *, permission="sandbox", session_id=None, options=None,
              mcp_config=None, prompt="Do the task.", model="test-model", **env_extra):
    """Render argv with the real builder, then run the adapter in place of `bin`."""
    prov = h.provider()
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    argv = prov.build_command(prompt=prompt, model=model, workdir=str(workdir),
                              permission=permission, session_id=session_id,
                              options=options or {}, timeout=600)
    if mcp_config is not None:
        argv += prov.mcp_launch({"mcp_command": "python3", "mcp_args": [],
                                 "mcp_argv": ["python3"], "mcp_env": {},
                                 "mcp_config": str(mcp_config)})["args"]
    env = h.base_env(tmp_path, fake, **env_extra)
    result = h.invoke(argv[1:], env, cwd=workdir)
    return prov, workdir, result


def the_exec(fake) -> dict:
    calls = fake.exec_calls()
    assert len(calls) == 1, f"expected exactly one `codex exec`, got {[c['argv'] for c in calls]}"
    return calls[0]


# ------------------------------------------------------------------ CX-C7 --

def test_cx_c7_adapter_is_executable_python_311_standard_library_only():
    h.require_adapter()
    source = h.ADAPTER.read_text()
    assert source.startswith("#!") and "python3" in source.splitlines()[0]
    tree = ast.parse(source, feature_version=(3, 11))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    foreign = sorted(imported - set(sys.stdlib_module_names) - {"__future__"})
    assert foreign == [], f"CX-C7: the adapter imports non-stdlib modules {foreign}"


def test_cx_c7_block_declares_the_shipped_fields():
    b = h.block()
    assert b.get("bin") == "codex"
    assert b.get("adapter") == "codex.py"
    assert b.get("family") == "codex"
    assert b.get("bin_versions_depth") == 3
    assert b.get("usage_mode") == "delta"
    assert b.get("models_parse") == "tsv"
    assert b.get("billing") == "plan"
    assert b.get("container_private_home") == [".codex"]
    assert isinstance(b.get("agent_guidance"), str) and b["agent_guidance"].strip()
    assert isinstance(b.get("notes"), str) and b["notes"].strip()


def test_cx_c7_block_has_no_home_links_no_transcript_and_no_models_cmd():
    b = h.block()
    # CX-D3: home_links would expose the user's auth.json and every session.
    assert "home_links" not in b
    assert "transcript" not in b
    # CX-C4 replaced: models come from the adapter's `models` action.
    assert "models_cmd" not in b


def test_cx_c8_block_sets_no_codex_home_through_env():
    # env: is expanded on the host and applied under every executor, so a
    # CODEX_HOME there would reach the container as an unmounted host path.
    assert "CODEX_HOME" not in (h.block().get("env") or {})


# ----------------------------------------------------------------- CX-C10 --

@pytest.mark.parametrize("permission,sandbox", [
    ("readonly", "read-only"), ("sandbox", "workspace-write"), ("full", "danger-full-access")])
@pytest.mark.parametrize("sid", [None, SID])
def test_cx_c10_real_builder_to_native_cli(tmp_path, fake, permission, sandbox, sid):
    fake.set(events=HAPPY)
    prompt = "--literal {task}\n$(do-not-execute) `no-shell`"
    prov, workdir, result = run_agent(tmp_path, fake, permission=permission, session_id=sid,
                                      options={"effort": "high"}, prompt=prompt)
    assert result.returncode == 0, result.stderr
    call = the_exec(fake)
    native = call["argv"]
    # Prompt on stdin, verbatim, and never on the command line.
    assert call["stdin"] == prompt
    assert prompt not in native
    assert call["cwd"] == str(workdir)
    assert "--json" in native
    assert "--ignore-user-config" in native
    assert h.sandbox_mode(native) == sandbox
    assert h.approval_policy(native) == "never"
    config = h.native_config(native)
    assert h.lookup(config, "model_reasoning_effort") == "high"
    assert h.lookup(config, "features.multi_agent") is False
    assert "test-model" in native
    ex = native.index("exec")
    if sid:
        assert "resume" in native[ex:]
        assert sid in native[native.index("resume") + 1:]
    else:
        assert "resume" not in native
    evs = h.events(prov, result.stdout)
    assert [e.text for e in evs if e.kind == TEXT] == ["done"]
    assert any(e.session_id == (sid or "thread-1") for e in evs)
    results = [e for e in evs if e.kind == RESULT]
    assert results and results[-1].status == "success"


def test_cx_c10_cached_input_is_separated_and_not_double_counted(tmp_path, fake):
    fake.set(events=HAPPY)
    prov, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    usage = [e.tokens for e in h.events(prov, result.stdout) if e.tokens]
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 30
    assert usage[0]["cache_read_input_tokens"] == 70
    assert usage[0]["output_tokens"] == 10
    assert token_count(usage[0]) == 110


def test_cx_c10_tokens_are_one_delta_per_turn_completed(tmp_path, fake):
    # usage_mode: delta sums every event's tokens, so a closing summary that
    # repeats the totals would count the run twice.
    fake.set(events=[
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "turn.completed",
         "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 10}},
        {"type": "turn.started"},
        {"type": "turn.completed",
         "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5}},
    ])
    prov, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    usage = [e.tokens for e in h.events(prov, result.stdout) if e.tokens]
    assert sum(token_count(u) for u in usage) == 165
    assert sum(u.get("cache_read_input_tokens", 0) for u in usage) == 70


@pytest.mark.parametrize("permission,sandbox", [
    ("readonly", "read-only"), ("sandbox", "workspace-write"), ("full", "danger-full-access")])
def test_cx_c10_local_permission_mapping(tmp_path, fake, permission, sandbox):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, permission=permission,
                             MULTIAGENTS_EXECUTOR="local")
    assert result.returncode == 0, result.stderr
    assert h.sandbox_mode(the_exec(fake)["argv"]) == sandbox


@pytest.mark.parametrize("permission", ["readonly", "sandbox", "full"])
def test_cx_c10_docker_interim_mapping_is_danger_full_access(tmp_path, fake, permission):
    # Interim, until live check L3. `tester` changes this test deliberately
    # after L3 if Codex's own sandbox initialises inside our container.
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, permission=permission,
                             MULTIAGENTS_EXECUTOR="docker",
                             MULTIAGENTS_PRIVATE_HOME=str(tmp_path / "private-home" / ".codex"))
    assert result.returncode == 0, result.stderr
    assert h.sandbox_mode(the_exec(fake)["argv"]) == "danger-full-access"


def test_cx_c10_every_native_invocation_disables_the_update_check(tmp_path, fake):
    fake.set(events=HAPPY, status="logged_in")
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"multiagents": {
        "command": "python3", "args": ["-m", "multiagents"], "env": {}}}}))
    _, _, result = run_agent(tmp_path, fake, mcp_config=config)
    assert result.returncode == 0, result.stderr
    env = h.base_env(tmp_path, fake)
    h.invoke(["check"], env)
    h.invoke(["login"], env)
    calls = fake.calls()
    assert any("exec" in c["argv"] for c in calls)
    assert any("status" in c["argv"] for c in calls)
    for call in calls:
        assert h.update_check_disabled(call["argv"]), \
            f"update check not disabled in {call['argv']} (see UPDATE_CHECK_OFF)"


def test_cx_c10_mcp_rendering_preserves_identity_and_escaping(tmp_path, fake):
    fake.set(events=HAPPY)
    prov = h.provider()
    path = tmp_path / "mcp.json"
    launch = prov.mcp_launch({
        "mcp_command": "/path with spaces/python", "mcp_args": ["-m", "multiagents"],
        "mcp_argv": ["/path with spaces/python", "-m", "multiagents"],
        "mcp_env": {"MULTIAGENTS_AGENT_ID": "a", "STRANGE": 'a"b\\c\n'},
        "mcp_config": str(path)})
    path.write_text(json.dumps(launch["config"]))
    workdir = tmp_path / "work"
    workdir.mkdir()
    argv = prov.build_command(prompt="p", model="m", workdir=str(workdir),
                              permission="sandbox") + launch["args"]
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CAN_SPAWN="1", **launch["env"])
    result = h.invoke(argv[1:], env, cwd=workdir)
    assert result.returncode == 0, result.stderr
    servers = h.lookup(h.native_config(the_exec(fake)["argv"]), "mcp_servers")
    ours = servers["multiagents"]
    assert ours["command"] == "/path with spaces/python"
    assert ours["args"] == ["-m", "multiagents"]
    assert ours["env"]["STRANGE"] == 'a"b\\c\n'
    assert ours["env"]["MULTIAGENTS_AGENT_ID"] == "a"
    assert ours.get("enabled", True) is True
    assert ours.get("required") is True


def test_cx_c10_without_mcp_config_no_server_is_enabled(tmp_path, fake):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    servers = h.lookup(h.native_config(the_exec(fake)["argv"]), "mcp_servers") or {}
    assert all(s.get("enabled") is False for s in servers.values()), servers


def test_cx_c10_inherited_mcp_servers_are_explicitly_disabled(tmp_path, fake):
    fake.set(events=HAPPY, mcp_list=json.dumps([
        {"name": "foreign.server", "transport": {"command": "false"}},
        {"name": "multiagents", "transport": {"url": "https://example.test/mcp"}}]))
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    servers = h.lookup(h.native_config(the_exec(fake)["argv"]), "mcp_servers")
    assert servers["foreign.server"]["enabled"] is False
    assert servers["multiagents"]["enabled"] is False


def test_cx_c10_inherited_server_disabled_but_ours_enabled(tmp_path, fake):
    fake.set(events=HAPPY, mcp_list=json.dumps([
        {"name": "foreign", "transport": {"command": "evil"}}]))
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"multiagents": {
        "command": "python3", "args": ["-m", "multiagents"], "env": {"A": "1"}}}}))
    _, _, result = run_agent(tmp_path, fake, mcp_config=config, MULTIAGENTS_CAN_SPAWN="1")
    assert result.returncode == 0, result.stderr
    servers = h.lookup(h.native_config(the_exec(fake)["argv"]), "mcp_servers")
    assert servers["foreign"]["enabled"] is False
    assert servers["multiagents"]["command"] == "python3"
    assert servers["multiagents"].get("enabled", True) is True


def test_cx_c10_unlistable_inherited_servers_refuse_the_run(tmp_path, fake):
    # They cannot be disabled if they cannot be enumerated (proposal, kept).
    fake.set(events=HAPPY, mcp_list_exit=1, mcp_list="boom")
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    assert fake.exec_calls() == []


def test_cx_c10_spawn_enabled_agent_without_mcp_fails_closed(tmp_path, fake):
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake, MULTIAGENTS_CAN_SPAWN="1")
    assert result.returncode != 0
    assert fake.exec_calls() == []


def test_cx_c10_one_tool_event_per_call_id_with_real_arguments(tmp_path, fake):
    item = {"id": "c", "type": "command_execution", "command": "ls src", "status": "in_progress"}
    done = {**item, "status": "completed", "aggregated_output": "a\nb"}
    fake.set(events=[
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.started", "item": item},
        {"type": "item.updated", "item": item},
        {"type": "item.completed", "item": done},
        {"type": "item.started", "item": {"id": "m", "type": "mcp_tool_call",
                                          "server": "multiagents", "tool": "agent_tree",
                                          "arguments": {"depth": 2}}},
        {"type": "future.event", "x": 1},
        {"type": "turn.completed", "usage": {}},
    ])
    prov, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    evs = h.events(prov, result.stdout)
    tools = [e for e in evs if e.kind == TOOL]
    assert [(t.name, t.args) for t in tools] == [
        ("command_execution", {"command": "ls src"}),
        ("mcp__multiagents__agent_tree", {"depth": 2}),
    ]
    assert tools[0].loop_signature() != tools[1].loop_signature()
    assert tools[0].turn and tools[0].turn == tools[1].turn
    assert any(e.kind == RAW and "future.event" in json.dumps(e.raw) for e in evs)


def test_cx_c10_only_completed_agent_messages_become_text(tmp_path, fake):
    fake.set(events=[
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "a", "type": "agent_message", "text": "do"}},
        {"type": "item.updated", "item": {"id": "a", "type": "agent_message", "text": "don"}},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ])
    prov, _, result = run_agent(tmp_path, fake)
    assert [e.text for e in h.events(prov, result.stdout) if e.kind == TEXT] == ["done"]


@pytest.mark.parametrize("evs,exitcode", [
    ([], 0),
    ([], 7),
    ([{"type": "turn.failed", "error": {"message": "quota reached"}}], 0),
    ([{"type": "turn.completed", "usage": {}}], 3),
    ([{"type": "turn.started"}], 0),
], ids=["nothing-exit0", "nothing-exit7", "turn-failed", "completed-then-exit3", "no-terminal"])
def test_cx_c10_failures_are_not_success(tmp_path, fake, evs, exitcode):
    fake.set(events=[{"type": "thread.started", "thread_id": "t"}, *evs], exit=exitcode)
    prov, _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert results, "a failed run must still end with a result event"
    assert results[-1].status != "success"


def test_cx_c10_failed_turn_reaches_the_engine_quota_detector(tmp_path, fake):
    fake.set(events=[{"type": "turn.failed", "error": {"message": "rate limit exceeded"}}])
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode != 0
    assert looks_like_quota_failure("failed", result.stderr)


def _resume_failed(prov, result) -> bool:
    lines = result.stderr.splitlines()
    texts = [e.text for e in h.events(prov, result.stdout)]
    return any(t.startswith(h.RESUME_FAILED) for t in lines + texts)


def test_cx_c10_resume_of_a_gone_thread_fails_loudly(tmp_path, fake):
    fake.set(events=HAPPY, resume_gone="error")
    prov, _, result = run_agent(tmp_path, fake, session_id=SID)
    assert result.returncode != 0
    assert _resume_failed(prov, result), (result.stdout, result.stderr)
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert not results or results[-1].status != "success"


def test_cx_c10_resume_that_silently_starts_a_new_thread_fails(tmp_path, fake):
    # The fake answers a resume by starting a DIFFERENT thread and finishing
    # happily. That is exactly the silent fresh start the contract forbids.
    fake.set(events=HAPPY, resume_gone="fresh")
    prov, _, result = run_agent(tmp_path, fake, session_id=SID)
    assert result.returncode != 0
    assert _resume_failed(prov, result), (result.stdout, result.stderr)
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert results and results[-1].status != "success"


def test_cx_c10_native_cli_is_found_through_multiagents_bin(tmp_path, fake):
    # The fake is not on PATH: only MULTIAGENTS_BIN leads to it.
    fake.set(events=HAPPY)
    _, _, result = run_agent(tmp_path, fake)
    assert result.returncode == 0, result.stderr
    assert len(fake.exec_calls()) == 1


# ----------------------------------------------------------------- CX-C13 --

@pytest.mark.parametrize("extra", [{}, {"MULTIAGENTS_UNATTENDED": "1", "MULTIAGENTS_RESUME": "1"}])
def test_cx_c13_launch_is_not_implemented(tmp_path, fake, extra):
    env = h.base_env(tmp_path, fake, MULTIAGENTS_ROLE="orchestrator",
                     MULTIAGENTS_PROJECT=str(tmp_path), **extra)
    result = h.invoke(["launch"], env)
    assert result.returncode == 64
    assert "not implemented" in result.stderr.lower()
    assert fake.calls() == []


@pytest.mark.parametrize("extra", [{}, {"MULTIAGENTS_COMPACT_CHECK": "1"}])
def test_cx_c13_compact_is_not_implemented(tmp_path, fake, extra):
    result = h.invoke(["compact"], h.base_env(tmp_path, fake, **extra))
    assert result.returncode == 64
    assert "not implemented" in result.stderr.lower()
    assert fake.calls() == []
