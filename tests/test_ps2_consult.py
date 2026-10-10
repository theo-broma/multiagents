"""PS2-R5: consult ownership through MCP tools and authenticated socket RPC."""
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c3_harness as h
from nc_fixture.world import World, call_tool, err_code
from nc_fixture.agent import task

BOUND = 4.0


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tick_seconds=0.05)
    world.max_wait = BOUND
    world.agent("implementer-deep", "fx", can_spawn=True, max_children=6)
    world.agent("dev-advisor", "fx", conversational=True, can_spawn=False)
    world.start_scheduler()
    yield world
    world.close()


def caller(w, monkeypatch, tag="CALLER"):
    node = w.simple(tag, "implementer-deep", fx={"gate": tag})
    launch = w.wait_spawn(tag, timeout=BOUND)
    run = w.until(lambda: w.get(node).get("runs"), BOUND)[-1]["run_id"]
    token = launch["mcp_env"].get("MULTIAGENTS_RPC_TOKEN") or launch["env_token"]
    h.as_subagent(monkeypatch, agent_id=run, can_spawn=True)
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    return run, token


def consult(w, message="ADVISOR"):
    return call_tool(w, "consult", "dev-advisor", task(message, text="advice"), timeout=2)


def container_caller(monkeypatch):
    # Public executor environment query: only the caller sees the container.
    from multiagents.executor.docker import DockerExecutor
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: True)


def owned_conversation(w, monkeypatch):
    run, token = caller(w, monkeypatch)
    first = consult(w)
    assert not first.get("error") and first.get("agent_id"), first
    return run, token, first["agent_id"]


@pytest.mark.parametrize("container_side", [False, True])
def test_ps2_r5_implementer_consult_and_resume_are_admitted(w, monkeypatch, container_side):
    if container_side:
        # Environment seam: a container MCP Runner cannot write host authority.
        # The real host scheduler remains in its separate, unpatched process.
        container_caller(monkeypatch)
    _, _, child = owned_conversation(w, monkeypatch)
    # A fresh MCP Runner is built by call_tool: relationship must survive it.
    second = consult(w, "ADVISOR-RESUMED")
    assert not second.get("error"), second
    assert second["agent_id"] == child
    assert "advice" in str(second)


def test_ps2_r5_second_caller_cannot_reuse_the_conversation(w, monkeypatch):
    container_caller(monkeypatch)
    _, _, child = owned_conversation(w, monkeypatch)
    _, token = caller(w, monkeypatch, "OTHER")
    result = consult(w, "STOLEN")
    error = result.get("error")
    if isinstance(error, str) and error.startswith("{"):
        error = json.loads(error)
    assert err_code({"ok": False, "error": error}) == "forbidden", result
    assert err_code(w.rpc("admit_run", {"run_id": child}, token=token)) == "forbidden"


@pytest.mark.parametrize("operation", ["steer_run", "stop_run"])
def test_ps2_r5_consult_record_does_not_authorize_steer_or_stop(w, monkeypatch, operation):
    container_caller(monkeypatch)
    _, token, child = owned_conversation(w, monkeypatch)
    args = {"run_id": child}
    if operation == "steer_run":
        args["message"] = "change the advisor"
    result = w.rpc(operation, args, token=token)
    assert err_code(result) == "forbidden", result


def test_ps2_r5_mutable_parent_alone_cannot_authorize_child_admission(w, monkeypatch):
    run, token = caller(w, monkeypatch)
    foreign = w.simple("FOREIGN", "worker", fx={"gate": "foreign"})
    w.wait_spawn("FOREIGN", timeout=BOUND)
    child = w.until(lambda: w.get(foreign).get("runs"), BOUND)[-1]["run_id"]
    # Container-writable tree.json is explicitly an untrusted input in R5.
    tree = json.loads(w.paths.tree_file.read_text())
    tree["nodes"][child]["parent"] = run
    w.paths.tree_file.write_text(json.dumps(tree))
    result = w.rpc("admit_run", {"run_id": child}, token=token)
    assert err_code(result) == "forbidden", result


def test_ps2_r5_advisor_has_no_spawn_capability(w, monkeypatch):
    container_caller(monkeypatch)
    caller(w, monkeypatch)
    # Use the capability while the real advisor process is still alive,
    # rather than confusing token revocation on exit with no spawn rights.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call_tool, w, "consult", "dev-advisor",
                             task("ADVISOR", gate="advisor", text="advice"), timeout=2)
        try:
            w.until(lambda: w.fx.by_tag("ADVISOR") or future.done(), BOUND)
            if not w.fx.by_tag("ADVISOR"):
                result = future.result(timeout=BOUND)
                assert not result.get("error"), result
                pytest.fail("consult returned without launching the advisor")
            launch = w.fx.by_tag("ADVISOR")[-1]
            token = launch["mcp_env"].get("MULTIAGENTS_RPC_TOKEN") or launch["env_token"]
            result = w.rpc("start_agent", {"agent": "worker", "task": "spawn"}, token=token)
            assert err_code(result) in ("forbidden", "unauthenticated"), result
        finally:
            w.gate("advisor")
            future.result(timeout=BOUND)
