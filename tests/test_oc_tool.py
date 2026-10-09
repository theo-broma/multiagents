"""OC — the MCP tools `request_compaction` / `compaction_status` and the
provider declaration: OC-R1, R6, R7, R8 (declaration side) and Decisions
1, 4, 8, 9, 10 (tool side).

Contract: `context/specs/orchestrator-compaction-request.md`. Written red,
before any implementation. No driver runs here: the tools are called as the
orchestrator's server runs them, against a project whose providers are fixtures
(`restart`, `in_session`, no declaration, an unknown mode).

What the contract fixes about a result, and so what is asserted: a successful
request carries `request_id` and `status == "pending"`; an unsupported one
carries `status == "unsupported"` and a `reason`; a refusal from a child names
`not_root`. Nothing about the shape of the status payload beyond `request_id`
and `status` is assumed.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import oc_harness as oc  # noqa: E402
from oc_harness import Oc, declaration, text  # noqa: E402,F401

from multiagents import driver, server  # noqa: E402
import test_phase0_interactive_compact as ic  # noqa: E402
from test_phase0_interactive_compact import session  # noqa: E402,F401


@pytest.fixture
def make(tmp_path, monkeypatch, capsys):
    def build(declare=None, **kw) -> Oc:
        return Oc(tmp_path, monkeypatch, capsys, declare=declare, **kw)
    yield build
    server._reset()


def requested(got: dict) -> bool:
    return got.get("status") == "pending" and bool(got.get("request_id"))


def nothing_recorded(s: Oc) -> None:
    got = s.status()
    assert "pending" not in text(got) and "request_id" not in text(got) or not got.get("request_id"), got


# ================================================================== OC-R1 ==

@pytest.mark.parametrize("mode", ["restart", "in_session"])
def test_oc_r1_a_root_call_records_a_pending_request(make, mode):
    s = make(declaration(mode))
    got = s.request("the plan changed")
    assert requested(got), got
    assert isinstance(got["request_id"], str)
    assert s.status(got["request_id"]).get("status") == "pending"


def test_oc_r1_the_reason_is_optional(make):
    s = make(declaration("restart"))
    got = tool_call_without_reason()
    assert requested(got), got


def tool_call_without_reason():
    return oc.tool_call("request_compaction")


def test_oc_r1_a_child_agent_is_refused_with_not_root_and_nothing_is_recorded(make, monkeypatch):
    s = make(declaration("restart"))
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-child")
    got = s.request("I am a child")
    assert "not_root" in text(got), got
    assert not got.get("request_id")
    monkeypatch.delenv("MULTIAGENTS_AGENT_ID")
    assert not s.status().get("request_id"), "a refused call left a request behind"


def test_oc_r1_a_second_call_while_pending_returns_the_same_id(make):
    s = make(declaration("restart"))
    first = s.request("first")
    second = s.request("second")
    assert requested(first) and requested(second)
    assert second["request_id"] == first["request_id"]


def test_oc_d9_the_first_reason_is_kept(make):
    s = make(declaration("restart"))
    first = s.request("because of alpha")
    s.request("because of beta")
    stored = text(s.status(first["request_id"]))
    assert "because of alpha" in stored and "because of beta" not in stored, stored


def test_oc_d9_concurrent_calls_share_one_id(make):
    s = make(declaration("restart"))
    ids, errors = [], []

    def go(n):
        try:
            ids.append(s.request(f"caller {n}")["request_id"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not errors, errors
    assert len(ids) == 8 and len(set(ids)) == 1, ids


# ============================================================ status (D10) ==

def test_oc_d10_status_without_id_is_the_latest_and_with_id_the_named_one(make):
    s = make(declaration("restart"))
    first = s.request("one")["request_id"]
    assert s.status().get("request_id") == first
    assert s.status(first).get("request_id") == first
    assert s.status(first).get("status") == "pending"


def test_oc_d10_status_with_nothing_recorded_reports_no_request(make):
    s = make(declaration("restart"))
    got = s.status()
    assert not got.get("request_id") and got.get("status") != "pending", got


def test_oc_d10_status_of_an_unknown_id_is_not_another_request(make):
    s = make(declaration("restart"))
    real = s.request("one")["request_id"]
    got = s.status("no-such-request-id")
    assert got.get("request_id") != real and got.get("status") != "pending", got


# ====================================================== OC-R6 / D1 / OC-R8 ==

def unsupported(got: dict) -> bool:
    return got.get("status") == "unsupported" and bool(str(got.get("reason", "")).strip())


def test_oc_r6_a_provider_without_the_declaration_is_unsupported_and_records_nothing(make):
    s = make(None)
    got = s.request("try")
    assert unsupported(got), got
    assert not got.get("request_id")
    assert not s.status().get("request_id")


@pytest.mark.parametrize("mode", ["", "background", "RESTART", "restart ", None, 3, ["restart"]])
def test_oc_r8_an_unknown_or_missing_mode_is_unsupported(make, mode):
    s = make({"compaction": {"mode": mode}})
    got = s.request("try")
    assert unsupported(got), got
    assert not s.status().get("request_id")


def test_oc_r8_a_declaration_without_a_mode_is_unsupported(make):
    s = make({"compaction": {}})
    assert unsupported(s.request("try"))


def test_oc_d1_a_declaration_of_none_is_unsupported(make):
    s = make({"compaction": None})
    assert unsupported(s.request("try"))


def test_oc_r6_a_call_with_no_driver_is_unsupported_and_records_nothing(make):
    """`--no-supervise` leaves no driver behind the CLI: no pid file."""
    s = make(declaration("restart"))
    driver._clear_pid(s.paths, "orchestrator")
    got = s.request("try")
    assert unsupported(got), got
    assert not s.status().get("request_id")


def test_oc_d8_a_dead_driver_is_unsupported(make):
    import subprocess
    s = make(declaration("restart"))
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    driver._write_pid(s.paths, "orchestrator", gone.pid)
    got = s.request("try")
    assert unsupported(got), got
    assert not s.status().get("request_id")


@pytest.mark.parametrize("mode", ["restart", "in_session"])
def test_oc_d8_a_supervised_initializer_root_may_request_compaction(make, monkeypatch, mode):
    import os
    import yaml

    s = make(declaration(mode))
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {
        "initializer": {"provider": "fakeprov", "model": "m", "launch": True,
                        "role": "initializer", "instructions": []}}}))
    s.tree.add(oc.Node(id="dr-initoc", agent="initializer", provider="fakeprov",
                       model="m", parent=None, depth=0, status="running",
                       task="initializer session", session=oc.OTHER_SID,
                       session_id=oc.OTHER_SID, role="initializer",
                       started_at=oc.tree_now()))
    monkeypatch.setenv("MULTIAGENTS_ROLE", "initializer")
    monkeypatch.setenv("MULTIAGENTS_SESSION_ID", oc.OTHER_SID)
    driver._write_pid(s.paths, "initializer", os.getpid())
    server._reset()

    got = s.request("bootstrap context needs compaction")
    assert requested(got), got
    assert "not_root" not in text(got) and "unsupported" not in text(got), got
    assert s.status(got["request_id"]).get("status") == "pending"


def test_oc_d8_a_session_with_no_binding_is_unsupported(make, monkeypatch):
    s = make(declaration("restart"))
    monkeypatch.delenv("MULTIAGENTS_SESSION_ID")
    got = s.request("try")
    assert unsupported(got), got


def test_oc_d8_a_session_the_driver_does_not_hold_is_unsupported(make, monkeypatch):
    s = make(declaration("restart"))
    monkeypatch.setenv("MULTIAGENTS_SESSION_ID", "11111111-2222-4333-8444-555555555555")
    got = s.request("try")
    assert unsupported(got), got


def test_oc_d1_the_shipped_claude_provider_declares_restart(tmp_path, monkeypatch, capsys):
    """Through the project's own roster: the orchestrator on the shipped
    `claude` provider is supported, with nothing declared by the project."""
    s = Oc(tmp_path, monkeypatch, capsys, declare=None)
    import yaml
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
        "provider": "claude", "model": "opus", "launch": True, "role": "orchestrator",
        "instructions": []}}}))
    server._reset()
    try:
        assert requested(s.request("shipped")), "claude must declare `restart`"
    finally:
        server._reset()


def test_oc_d1_the_declaration_is_inherited_through_extends(make):
    import yaml
    s = make(declaration("restart"))
    base = s.config.providers["fakeprov"]
    child = {"extends": "fakeprov", "env": base.get("env", {})}
    (s.paths.config / "providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"fakeprov": base, "fakechild": child}}))
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
        "provider": "fakechild", "model": "m", "launch": True, "role": "orchestrator",
        "instructions": []}}}))
    server._reset()
    assert requested(s.request("inherited")), "an extending provider inherits `compaction`"


def test_oc_d1_an_extending_provider_may_override_the_mode_to_unsupported(make):
    import yaml
    s = make(declaration("restart"))
    base = s.config.providers["fakeprov"]
    child = {"extends": "fakeprov", "compaction": {"mode": "unheard_of"}}
    (s.paths.config / "providers.yaml").write_text(yaml.safe_dump(
        {"providers": {"fakeprov": base, "fakechild": child}}))
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
        "provider": "fakechild", "model": "m", "launch": True, "role": "orchestrator",
        "instructions": []}}}))
    server._reset()
    assert unsupported(s.request("overridden"))


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_oc_r6_support_follows_the_declaration_never_the_provider_name(make, name):
    """A fixture that merely carries a shipped provider's name is judged by what
    it declares: nothing declared, nothing supported."""
    import yaml
    s = make(None)
    data = dict(s.config.providers["fakeprov"])
    (s.paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {name: data}}))
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
        "provider": name, "model": "m", "launch": True, "role": "orchestrator",
        "instructions": []}}}))
    server._reset()
    assert unsupported(s.request("by name")), f"{name!r} was supported without declaring it"


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_oc_r6_a_declaration_on_a_shipped_name_is_enough(make, name):
    import yaml
    s = make(declaration("in_session"))
    data = dict(s.config.providers["fakeprov"])
    (s.paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {name: data}}))
    (s.paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {"orchestrator": {
        "provider": name, "model": "m", "launch": True, "role": "orchestrator",
        "instructions": []}}}))
    server._reset()
    assert requested(s.request("by declaration"))


# ======================================================= D4: the storage ==

def test_oc_d4_a_request_is_not_stored_in_the_agent_writable_launch_dir(make):
    s = make(declaration("restart"))
    request_id = s.request("secret reason zebra")["request_id"]
    launch = s.paths.data / "launch"
    for path in launch.rglob("*") if launch.exists() else []:
        if path.is_file():
            body = path.read_bytes()
            assert request_id.encode() not in body and b"secret reason zebra" not in body, path


def test_oc_d4_a_resume_of_the_same_session_keeps_the_request(make):
    s = make(declaration("restart"))
    first = s.request("kept")["request_id"]
    server._reset()                      # a new server process for the same session
    again = s.request("kept again")
    assert again.get("request_id") == first, again
    assert s.status().get("request_id") == first


def test_oc_d4_a_fresh_session_never_inherits_a_request(make, monkeypatch):
    s = make(declaration("restart"))
    first = s.request("old session")["request_id"]
    server._reset()
    monkeypatch.setenv("MULTIAGENTS_SESSION_ID", oc.OTHER_SID)
    s.tree.add(oc.Node(id="dr-f8f001", agent="orchestrator", provider="fakeprov",
                       model="m", parent=None, depth=0, status="running",
                       task="orchestrator session", session=oc.OTHER_SID,
                       session_id=oc.OTHER_SID, role="orchestrator",
                       started_at=oc.tree_now()))
    assert s.status().get("request_id") != first
    fresh = s.request("new session")
    assert requested(fresh) and fresh["request_id"] != first, fresh


# ================================================================ OC-R4/R7 ==

def test_oc_r4_the_tool_documentation_says_typed_but_unsent_text_is_invisible():
    assert callable(getattr(server, "request_compaction", None)), "request_compaction is not implemented"
    doc = " ".join((server.request_compaction.__doc__ or "").split()).lower()
    assert "typed" in doc, "the documentation must say typed text is invisible to the driver"
    assert "sent" in doc or "submit" in doc or "send" in doc


README = Path(__file__).resolve().parents[1] / "src/multiagents/defaults/providers/README.md"


def readme() -> str:
    return " ".join(README.read_text().split())


def test_oc_r7_the_provider_documentation_describes_the_declaration_and_both_modes():
    doc = readme()
    assert "compaction:" in doc or "compaction: {" in doc
    assert "restart" in doc and "in_session" in doc


def test_oc_r7_the_provider_documentation_states_the_entry_point_contract():
    """Inputs (the session id and the request id), success and failure exit
    codes, and the timeout."""
    doc = readme()
    start = doc.find("request_id")
    assert start >= 0, "the entry point's request_id input is not documented"
    assert "MULTIAGENTS_SESSION_ID" in doc
    assert "idempotent" in doc.lower()
    assert "exit 0" in doc and "exit 64" in doc
    assert "compact_timeout_seconds" in doc or "timeout" in doc.lower()
