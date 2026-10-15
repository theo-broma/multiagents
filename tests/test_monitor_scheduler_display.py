"""Regressions for context/plans/2026-10-15-monitor-scheduler-display.md."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from multiagents.scheduler import overview, sessions

PAGE = Path(__file__).resolve().parents[1] / "src/multiagents/monitor/page.html"


def render(section):
    """Execute the shipped panel and DOM builder; retain the details tree."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    page = PAGE.read_text()
    builder = page.split("const el = ", 1)[1].split("\n\nconst api", 1)[0]
    panel = page.split("function blockedText", 1)[1].split("\nfunction renderLive", 1)[0]
    details = page.split("function details", 1)[1].split("\nfunction conversationCard", 1)[0]
    script = r'''
function element(tag) {
  return {nodeType: 1, tag, attrs: {}, children: [], listeners: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, v) { this.listeners[k] = v; },
    append(v) { this.children.push(v); }};
}
const document = {createElement: element,
  createTextNode: text => ({nodeType: 3, text})};
const OPEN_DETAILS = new Set();
'''
    script += "const el = " + builder + "\nfunction details" + details
    script += "\nfunction blockedText" + panel
    script += "\nconst section = " + json.dumps(section) + ";"
    script += r'''
const tree = schedulerPanel(section);
function walk(n) { return [n, ...(n.children || []).flatMap(walk)]; }
const disclosure = walk(tree).find(n => n.tag === "details");
let toggleKept = null;
if (disclosure) {
  disclosure.listeners.toggle({target: {open: true}});
  toggleKept = walk(schedulerPanel(section)).find(n => n.tag === "details").attrs.open;
}
console.log(JSON.stringify({tree, toggleKept}));
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def text(tree, *, expanded=False):
    if tree.get("tag") == "details" and not expanded:
        return text(tree["children"][0])
    return str(tree.get("text", "")) + " ".join(
        text(child, expanded=expanded) for child in tree.get("children", []))


def section(nodes, aliases=()):
    return {"running": True, "pid": 123, "last_tick": None, "counts": {},
            "starving": [], "locks": [], "nodes": nodes, "aliases": list(aliases)}


@pytest.mark.parametrize("surface", ["page", "doctor"])
@pytest.mark.parametrize("declared,opened,next_change,expected", [
    (False, True, None, None),
    (True, True, None, "no next close in forecast range"),
    (True, False, None, "no next open in forecast range"),
    (True, True, "2026-10-16T17:00:00+00:00", "2026-10-16T17:00:00+00:00"),
    (True, False, "2026-10-16T09:00:00+00:00", "2026-10-16T09:00:00+00:00"),
])
def test_window_declaration_and_forecast(surface, declared, opened, next_change, expected):
    row = {"node_id": "nd-example", "state": "open", "blocked": [],
           "window": {"open": opened, "next_open": None, "next_close": None}}
    row["window"]["next_close" if opened else "next_open"] = next_change
    if declared:
        row["window_declared"] = {"days": ["mon"], "ranges": ["09:00-17:00"]}
    sec = section([row])
    output = (text(render(sec)["tree"]) if surface == "page"
              else "\n".join(overview.doctor_lines(sec)[0]))
    assert "?" not in output
    if expected is None:
        assert "window " not in output
    else:
        assert expected in output


@pytest.fixture
def alias_section(tmp_path, monkeypatch):
    nodes = [{"id": "nd-loop", "state": "done", "parent": None,
              "template": {"instance": "12345678-abcd-4321-9876-123456789abc"}}]
    for state in ("open", "running", "suspended", "held", "done", "cancelled"):
        nodes.append({"id": f"nd-{state}", "state": state, "parent": "nd-loop",
                      "session": state.upper(), "agent": "worker"})
    # A finished node sharing an active alias must not hide or duplicate it.
    nodes.append({"id": "nd-shared-done", "state": "done", "parent": "nd-loop", "session": "OPEN"})
    bindings = [{"id": sessions.alias_id(nodes[1], {n["id"]: n for n in nodes}),
                 "alias": "OPEN", "instance": nodes[0]["template"]["instance"],
                 "provider": "fx", "account": "a", "model": "m1", "session_id": "real-session"}]
    monkeypatch.setattr(overview, "_process", lambda root: {"pid": 123})
    monkeypatch.setattr(overview, "_ask", lambda root: ({"aliases": bindings}, nodes))
    return overview.section(tmp_path)


@pytest.mark.parametrize("state", ["open", "running", "suspended", "held"])
def test_active_alias_on_its_node_row(alias_section, state):
    tree = render(alias_section)["tree"]
    rows = [child for child in tree["children"] if f"nd-{state}" in text(child)]
    assert len(rows) == 1
    assert f"{state.upper()}@12345678" in text(rows[0])
    assert "12345678-abcd" not in text(rows[0])
    assert "provider session" in text(rows[0])


def test_finished_aliases_are_reachable_through_persistent_toggle(alias_section):
    result = render(alias_section)
    collapsed = text(result["tree"])
    assert "+2 finished sessions" in collapsed
    assert "DONE@" not in collapsed and "CANCELLED@" not in collapsed
    expanded = text(result["tree"], expanded=True)
    assert "nd-done" in expanded and "DONE@12345678" in expanded
    assert "nd-cancelled" in expanded and "CANCELLED@12345678" in expanded
    assert result["toggleKept"] == "open"
    assert collapsed.count("OPEN@12345678") == 1


def test_api_section_keeps_every_alias_and_all_node_associations(alias_section):
    assert len(alias_section["aliases"]) == 6
    aliases = {a["alias"]: a for a in alias_section["aliases"]}
    assert aliases["OPEN"]["nodes"] == ["nd-open", "nd-shared-done"]
    assert aliases["DONE"]["nodes"] == ["nd-done"]
    assert aliases["CANCELLED"]["nodes"] == ["nd-cancelled"]


@pytest.mark.parametrize("session_id", [None, "", "provider-session"])
def test_bound_means_a_provider_session_exists(session_id):
    aliases = overview._aliases([], [{"id": "binding", "alias": "A", "session_id": session_id}], None)
    assert aliases[0]["bound"] is bool(session_id)
    aliases[0].update(instance="12345678-rest", nodes=["nd-active"])
    sec = section([{"node_id": "nd-active", "state": "open", "blocked": []}], aliases)
    output = text(render(sec)["tree"])
    assert ("bound — a real provider session exists" if session_id
            else "unbound — no provider session created yet") in output


def test_doctor_shows_active_aliases_with_nodes_and_finished_count(alias_section):
    info, _ = overview.doctor_lines(alias_section)
    output = "\n".join(info)
    assert "OPEN@12345678 (nd-open): bound — a real provider session exists" in output
    assert "RUNNING@12345678 (nd-running): unbound — no provider session created yet" in output
    assert "+2 finished sessions" in output
    assert "DONE@" not in output and "CANCELLED@" not in output


def test_live_monitor_page_has_no_phantom_window(tmp_path, monkeypatch):
    from test_nc_m6_monitor import Monitor, bounded_waits
    from nc_fixture.gitworld import GitWorld
    from nc_fixture.agent import task

    world = bounded_waits(GitWorld(tmp_path, monkeypatch))
    monitor = Monitor(world, monkeypatch)
    try:
        world.start_scheduler()
        world.ok("instantiate_template", {
            "name": "implement", "params": {
                "spec_path": "context/specs/display.md", "tests_task": task("DISPLAY", gate="gDISPLAY"),
                "implement_task": task("IMPL"), "tester": "worker", "reviewer": "worker",
                "implementer": "worker", "test_rounds": 2, "impl_rounds": 3}})
        world.until(lambda: world.fx.by_tag("DISPLAY"), what="live aliased launch")
        sec = monitor.section()
        status, page = monitor.get("/")
        assert status == 200 and "function schedulerSession" in page
        output = text(render(sec)["tree"])
        assert "window " not in output
        live_ids = {n["node_id"] for n in sec["nodes"]
                    if n["state"] in ("open", "running", "suspended", "held")}
        for alias in sec["aliases"]:
            if live_ids.intersection(alias["nodes"]):
                assert f"{alias['alias']}@{alias['instance'][:8]}" in output
        print("Live scheduler panel:", output)
    finally:
        monitor.close()
        world.close()
