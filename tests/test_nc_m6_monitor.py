"""M6 — NC-R43 (monitor state API and page, `doctor`, with the gate on),
NC-R44 (the shipped orchestrator brief gains the node-planning section) and
the doctor half of NC-R55.

The monitor is the real `Handler` on an ephemeral port (as test_c20 does); the
payload is read from `/api/state`. `doctor` is `cli.cmd_doctor` run in-process
with the provider probes stubbed (as test_nc_m1_config does). The orchestrator
prompt is the assembled one printed by `multiagents prompt orchestrator`, the
way the C13 PN-R3 tests read it.

THE CONTRACT DOES NOT FIX THE PAYLOAD'S SHAPE. Assumed, and the only
assumptions made here (change them in the block below, and only there):
- `/api/state` gains a top-level key `scheduler` when the gate is on, and the
  scheduler section is a JSON object (NC-R43 "scheduler state (NC-R16 status)").
- inside it everything is found by *key-name fragment* anywhere in the tree:
  a key containing `lock` carries the locks and their holders, `alias` or
  `binding` the session aliases, `window` the windows, `starv` the starving
  nodes. The ids of nodes appear as `id` or `node_id` strings.
- a node is described by a mapping with `id`/`node_id` and its derived blocked
  reasons somewhere inside it as `{code, detail}` or plain strings.
- "held first": among the node mappings met in document order (leaving out
  the lock/alias/window/starving sub-sections) held nodes precede the others.
- a dead scheduler shows as a false-y `running`/`alive`/`up` flag, or a
  `state`/`status` string among dead/down/stopped/not_running/unavailable.
- a window's next open/close is a time (ISO-8601 string or epoch number)
  somewhere under the node's window mapping.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.clock import ALL_DAYS, ClockWorld, local, utc  # noqa: E402
from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import (World, blocked_codes, call_tool, unwrap,  # noqa: E402
                              write_tree_entries)
from multiagents import cli, manifest  # noqa: E402
from multiagents.config import load as load_config  # noqa: E402
from multiagents.monitor import server  # noqa: E402

# --------------------------------------------------------------------- shape
SCHEDULER_KEY = "scheduler"
LOCK_FRAGMENT = "lock"
ALIAS_FRAGMENTS = ("alias", "binding")
WINDOW_FRAGMENT = "window"
STARVING_FRAGMENT = "starv"
NODE_ID = re.compile(r"nd-[0-9a-f]{8}")
DEAD_WORDS = {"dead", "down", "stopped", "not_running", "unavailable", "not running"}
SIDE_SECTIONS = (LOCK_FRAGMENT, *ALIAS_FRAGMENTS, WINDOW_FRAGMENT, STARVING_FRAGMENT)


def walk(value, path=()):
    """Every (path-of-keys, value) pair, depth first, in document order."""
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, path + (str(k),))
    elif isinstance(value, list):
        for v in value:
            yield from walk(v, path)


def under(section, fragments) -> list:
    """The values of every key whose name contains one of `fragments`."""
    out = []
    for path, value in walk(section):
        if path and any(f in path[-1].lower() for f in fragments):
            out.append(value)
    return out


def text_of(value) -> str:
    return json.dumps(value, sort_keys=False, default=str)


def node_rows(section) -> list[dict]:
    """Node mappings in document order, outside the side sections."""
    rows, seen = [], set()
    for path, value in walk(section):
        if any(f in k.lower() for k in path for f in SIDE_SECTIONS):
            continue
        if isinstance(value, dict):
            ident = value.get("id") or value.get("node_id")
            if isinstance(ident, str) and NODE_ID.fullmatch(ident) and ident not in seen:
                seen.add(ident)
                rows.append(value)
    return rows


def row_of(section, node_id: str) -> dict:
    for row in node_rows(section):
        if (row.get("id") or row.get("node_id")) == node_id:
            return row
    raise AssertionError(f"{node_id} is not listed in the scheduler section: {text_of(section)[:1500]}")


def says_down(section) -> bool:
    for path, value in walk(section):
        if path and path[-1] in ("running", "alive", "up") and value is False:
            return True
        if path and path[-1] in ("state", "status") and isinstance(value, str) \
                and value.lower() in DEAD_WORDS:
            return True
    return False


def instants(value) -> list[datetime]:
    out = []
    for _, v in walk(value):
        if isinstance(v, str):
            try:
                d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
            if d.tzinfo:
                out.append(d)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e9:
            out.append(datetime.fromtimestamp(v, tz=utc(1970, 1, 1).tzinfo))
    return out



# A red test must fail in seconds: the fixture's default waits (30 s, 45 s) are
# capped here; waits that pass an explicit timeout keep it.
WAIT_BOUND = 10


def bounded_waits(world):
    """Give the world's default-timeout waits a short bound; returns the world."""
    real_until, real_state, real_spawn = world.until, world.wait_state, world.wait_spawn

    def until(pred, timeout=WAIT_BOUND, step=0.1, what="the condition", **kw):
        return real_until(pred, timeout, step, what, **kw)

    def wait_state(node_id, state, timeout=WAIT_BOUND, **kw):
        return real_state(node_id, state, timeout, **kw)

    def wait_running(node_id, timeout=WAIT_BOUND, **kw):
        return real_state(node_id, "running", timeout, **kw)

    def wait_spawn(tag, fx=None, timeout=WAIT_BOUND):
        return real_spawn(tag, fx, timeout)

    world.until, world.wait_state, world.wait_running = until, wait_state, wait_running
    world.wait_spawn = wait_spawn
    if hasattr(world, "done"):
        world.done = lambda node_id, timeout=WAIT_BOUND: real_state(node_id, "done", timeout)
    return world

# ------------------------------------------------------------------ monitor

class Monitor:
    def __init__(self, world: World, monkeypatch):
        self.world = world
        self.token = "tok-nc-m6"
        monkeypatch.setattr(server.Handler, "paths", world.paths)
        monkeypatch.setattr(server.Handler, "token", self.token)
        monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, route: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            conn.request("GET", f"{route}{'&' if '?' in route else '?'}"
                         f"token={urllib.parse.quote(self.token)}")
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def state(self) -> dict:
        status, body = self.get("/api/state?scripts=0")
        assert status == 200, body[:500]
        return json.loads(body)

    def section(self) -> dict:
        state = self.state()
        assert isinstance(state.get(SCHEDULER_KEY), dict), \
            f"/api/state has no `{SCHEDULER_KEY}` section: {sorted(state)}"
        return state[SCHEDULER_KEY]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = bounded_waits(GitWorld(tmp_path, monkeypatch))
    mon = Monitor(world, monkeypatch)
    world.mon = mon
    yield world
    mon.close()
    world.close()


@pytest.fixture
def cw(tmp_path, monkeypatch):
    world = bounded_waits(ClockWorld(tmp_path, monkeypatch, now=local(2026, 10, 10, 12, 0)))   # a Saturday
    mon = Monitor(world, monkeypatch)
    world.mon = mon
    yield world
    mon.close()
    world.close()


def held_node(world: World) -> str:
    """A `held` node (reason admission:refused) made by migrating a refused
    legacy queue entry; call BEFORE the scheduler starts."""
    def build(tree):
        e = tree.defer({"agent": "worker", "task": task("REFUSED"), "timeout": None,
                        "model": None, "workdir": None, "provider": "fx"},
                       9e9, "quota window")
        with tree.transaction() as data:
            for d in data["deferred"]:
                if d["id"] == e["id"]:
                    d["status"] = "refused"
    write_tree_entries(world, build)
    return ""


def find_by_tag(world: World, tag: str) -> str:
    for n in world.list():
        if f"[{tag}]" in (n.get("task") or ""):
            return n["id"]
    raise AssertionError(f"no node tagged {tag}")


# ================================================================== NC-R43

def test_nc_r43_the_state_api_carries_a_scheduler_section_with_the_scheduler_status(w):
    pid = w.start_scheduler()
    w.simple("A", fx={"gate": "gA"})
    sec = w.mon.section()
    assert pid in [v for _, v in walk(sec) if isinstance(v, int)], text_of(sec)[:800]
    assert says_down(sec) is False
    counts = [v for p, v in walk(sec) if p and p[-1] in ("counts", "by_state") and isinstance(v, dict)]
    assert counts and any(c.get("running") == 1 or c.get("open") == 1 for c in counts), counts


def test_nc_r43_every_node_is_listed_with_its_state(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "gA"})
    w.wait_running(a)
    b = w.simple("B", locks=["L"], depends_on=[{"node": a}])
    done = w.simple("C")
    w.wait_state(done, "done")
    sec = w.mon.section()
    states = {i: (row_of(sec, i).get("state")) for i in (a, b, done)}
    assert states == {a: "running", b: "open", done: "done"}, states


def test_nc_r43_a_blocked_node_shows_its_derived_reason_and_it_is_not_stored(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "gA"})
    w.wait_running(a)
    b = w.simple("B", depends_on=[{"node": a}])
    w.until(lambda: "dependency" in blocked_codes(w.get(b)), what="B blocked")
    row = row_of(w.mon.section(), b)
    assert "dependency" in text_of(row), row
    assert a in text_of(row), "the reason names what it waits for"


def test_nc_r43_held_nodes_are_listed_before_every_other_node(w):
    held_node(w)
    w.start_scheduler()
    older = w.simple("OLD", fx={"gate": "gOLD"})
    w.wait_running(older)
    open_ = w.simple("OPEN", depends_on=[{"node": older}])
    held = [n["id"] for n in w.list() if n.get("state") == "held"]
    assert len(held) == 1, w.list()
    ids = [(r.get("id") or r.get("node_id")) for r in node_rows(w.mon.section())]
    assert ids.index(held[0]) == 0, ids
    assert set(ids) >= {older, open_, held[0]}


def test_nc_r43_a_held_node_shows_its_hold_reason(w):
    held_node(w)
    w.start_scheduler()
    held = [n for n in w.list() if n.get("state") == "held"][0]
    row = row_of(w.mon.section(), held["id"])
    assert "admission:refused" in text_of(row) or "refused" in text_of(row), row


def test_nc_r43_locks_are_shown_with_their_holder_and_the_node_waiting_on_them(w):
    w.start_scheduler()
    holder = w.simple("H", locks=["runner.py"], fx={"gate": "gH"})
    w.wait_running(holder)
    waiter = w.simple("W", locks=["runner.py"])
    w.until(lambda: "lock" in blocked_codes(w.get(waiter)), what="W blocked by lock")
    sec = w.mon.section()
    locks = under(sec, (LOCK_FRAGMENT,))
    mentions = [text_of(v) for v in locks]
    assert any("runner.py" in m and holder in m for m in mentions), mentions
    assert "lock" in text_of(row_of(sec, waiter))


def test_nc_r43_a_released_lock_is_no_longer_shown_as_held(w):
    w.start_scheduler()
    holder = w.simple("H", locks=["runner.py"], fx={"gate": "gH"})
    w.wait_running(holder)
    w.gate("gH")
    w.wait_state(holder, "done")
    sec = w.mon.section()
    held_locks = [text_of(v) for v in under(sec, (LOCK_FRAGMENT,))]
    assert not any("runner.py" in m and holder in m for m in held_locks), held_locks


def test_nc_r43_session_aliases_and_their_frozen_binding_are_shown(w):
    """Needs M4 (templates); the monitor part is M6."""
    w.start_scheduler()
    spec = w.ok("instantiate_template", {
        "name": "implement",
        "params": {"spec_path": "context/specs/x.md",
                   "tests_task": task("TESTS", gate="gT"), "implement_task": task("IMPL"),
                   "tester": "worker", "reviewer": "worker", "implementer": "worker",
                   "test_rounds": 2, "impl_rounds": 3}})
    top = unwrap(spec)["id"] if isinstance(unwrap(spec), dict) and unwrap(spec).get("id") else \
        [n for n in w.list() if n.get("template")][0]["id"]
    w.until(lambda: w.fx.by_tag("TESTS"), what="the first aliased launch")
    sec = w.mon.section()
    shown = [text_of(v) for v in under(sec, ALIAS_FRAGMENTS)]
    assert shown, text_of(sec)[:800]
    assert any("fx" in s and "fx/m1" in s for s in shown), shown
    instance = w.get(top)["template"]["instance"]
    assert any(str(instance) in s for s in shown), shown


def test_nc_r43_a_closed_window_shows_the_next_open_and_an_open_window_the_next_close(cw):
    cw.start_scheduler()
    weekdays = {"days": ["mon", "tue", "wed", "thu", "fri"], "ranges": ["09:00-17:00"],
                "timezone": "Europe/Paris"}
    closed = cw.simple("CLOSED", window=weekdays)
    always = cw.simple("OPEN", window={"days": ALL_DAYS, "ranges": ["08:00-18:00"],
                                       "timezone": "Europe/Paris"}, fx={"gate": "gO"})
    cw.wait_running(always)
    sec = cw.mon.section()
    next_open = local(2026, 10, 12, 9, 0)                 # Monday
    closing = local(2026, 10, 10, 18, 0)                  # today's close
    closed_times = instants(under(row_of(sec, closed), (WINDOW_FRAGMENT,)) or row_of(sec, closed))
    open_times = instants(under(row_of(sec, always), (WINDOW_FRAGMENT,)) or row_of(sec, always))
    assert next_open in closed_times, (closed_times, text_of(row_of(sec, closed)))
    assert closing in open_times, (open_times, text_of(row_of(sec, always)))
    assert "window" in text_of(row_of(sec, closed))


def test_nc_r43_a_node_outside_its_window_shows_the_window_reason(cw):
    cw.start_scheduler()
    a = cw.simple("A", window={"days": ["mon"], "ranges": ["09:00-10:00"]})
    cw.until(lambda: "window" in cw.codes(a), what="A outside its window")
    assert "window" in text_of(row_of(cw.mon.section(), a))


def test_nc_r43_a_node_ready_for_longer_than_the_threshold_is_listed_as_starving(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=utc(2026, 10, 12, 8, 0),
                       scheduler={"starvation_after_seconds": 3600})
    mon = Monitor(world, monkeypatch)
    try:
        world.start_scheduler()
        holder = world.hold_lock("L")
        waiter = world.simple("W", locks=["L"])
        fresh = world.simple("F", locks=["L"])
        world.until(lambda: "lock" in world.codes(waiter), what="W blocked")
        world.set_clock(utc(2026, 10, 12, 8, 30))
        sec = mon.section()
        assert not any(waiter in text_of(v) for v in under(sec, (STARVING_FRAGMENT,)))
        world.set_clock(utc(2026, 10, 12, 10, 0))
        sec = mon.section()
        starving = [text_of(v) for v in under(sec, (STARVING_FRAGMENT,))]
        assert any(waiter in s and fresh in s for s in starving), starving
        assert not any(holder in s for s in starving), starving
    finally:
        mon.close()
        world.close()


def test_nc_r43_a_dead_scheduler_with_the_gate_on_is_shown_as_dead_not_hidden(w):
    w.start_scheduler()
    w.simple("A", fx={"gate": "gA"})
    w.stop_scheduler()
    sec = w.mon.section()
    assert says_down(sec), text_of(sec)[:800]


def test_nc_r43_the_state_api_survives_a_scheduler_that_was_never_started(w):
    w.write_config()
    state = w.mon.state()
    assert state["running"] == [] and "alerts" in state          # the old payload intact
    assert isinstance(state.get(SCHEDULER_KEY), dict)
    assert says_down(state[SCHEDULER_KEY])


def test_nc_r43_reading_the_state_changes_nothing(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "gA"})
    w.wait_running(a)
    before = (w.get(a)["revision"], w.plan_revision(), len(w.fx.calls()))
    for _ in range(3):
        w.mon.state()
    assert (w.get(a)["revision"], w.plan_revision(), len(w.fx.calls())) == before


def test_nc_r43_the_monitor_never_shows_a_capability_token(w):
    w.start_scheduler()
    token = w.root_token()
    w.simple("A", fx={"gate": "gA"})
    status, body = w.mon.get("/api/state")
    assert status == 200 and token not in body
    status, page = w.mon.get("/")
    assert token not in page


def test_nc_r43_the_page_renders_the_scheduler_section(w):
    w.start_scheduler()
    status, page = w.mon.get("/")
    assert status == 200
    assert SCHEDULER_KEY in page.lower() and "scheduler" in page.lower()
    assert re.search(r"\bs(tate)?\.scheduler\b|\[['\"]scheduler['\"]\]|\bscheduler\s*[:=]", page), \
        "the page script does not read the `scheduler` key of the state"


def test_nc_r43_the_page_marks_held_and_starving_nodes(w):
    status, page = w.mon.get("/")
    assert status == 200
    assert "held" in page.lower() and "starv" in page.lower()


# ------------------------------------------------------------------- doctor

@pytest.fixture
def doctor(monkeypatch, capsys):
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *a: {})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *a: {})
    monkeypatch.setattr(cli, "read_all", lambda *a: {})
    monkeypatch.setattr(cli, "_report_agents", lambda *a: 0)
    monkeypatch.setattr(cli, "find_shadowing", lambda *a: [])
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *a: 0)

    def run(world: World):
        capsys.readouterr()
        code = cli.cmd_doctor(argparse.Namespace(path=str(world.root), clear=None, force=False))
        out = capsys.readouterr().out
        match = re.search(r"^(\d+) problem\(s\)$", out, re.M)
        return code, int(match.group(1)) if match else 0, out

    return run


def test_nc_r43_doctor_reports_a_dead_scheduler_while_the_gate_is_on_as_one_problem(w, doctor):
    w.start_scheduler()
    _, base, out_ok = doctor(w)
    w.stop_scheduler()
    code, problems, out = doctor(w)
    assert problems == base + 1, out
    assert code == 1
    line = [ln for ln in out.splitlines() if "scheduler" in ln.lower() and "!" in ln]
    assert line, out


def test_nc_r43_doctor_is_clean_about_a_live_scheduler(w, doctor):
    w.start_scheduler()
    _, problems, out = doctor(w)
    assert not [ln for ln in out.splitlines() if ln.strip().startswith("!") and "scheduler" in ln.lower()], out


def test_nc_r43_doctor_with_the_gate_off_does_not_mention_a_missing_scheduler(tmp_path, monkeypatch, doctor):
    world = World(tmp_path, monkeypatch, gate=False)
    try:
        world.write_config()
        _, problems, out = doctor(world)
        assert not [ln for ln in out.splitlines() if "scheduler" in ln.lower() and "!" in ln], out
    finally:
        world.close()


def test_nc_r43_doctor_lists_nodes_by_state_with_held_first(w, doctor):
    held_node(w)
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "gA"})
    w.wait_running(a)
    b = w.simple("B", depends_on=[{"node": a}])
    held = [n["id"] for n in w.list() if n.get("state") == "held"][0]
    _, _, out = doctor(w)
    assert held in out and a in out and b in out, out
    assert out.index(held) < out.index(a) and out.index(held) < out.index(b)
    assert "refused" in out
    line_b = [ln for ln in out.splitlines() if b in ln]
    assert any("dependency" in ln for ln in line_b), line_b


def test_nc_r43_doctor_shows_locks_with_their_holders(w, doctor):
    w.start_scheduler()
    holder = w.simple("H", locks=["runner.py"], fx={"gate": "gH"})
    w.wait_running(holder)
    _, _, out = doctor(w)
    lines = [ln for ln in out.splitlines() if "runner.py" in ln]
    assert any(holder in ln for ln in lines), out


def test_nc_r43_doctor_shows_the_next_open_of_a_closed_window(cw, doctor):
    cw.start_scheduler()
    cw.simple("CLOSED", window={"days": ["mon"], "ranges": ["09:00-17:00"],
                                "timezone": "Europe/Paris"})
    _, _, out = doctor(cw)
    assert re.search(r"2026-10-12[T ]09:00|2026-10-12[T ]07:00", out), out


def test_nc_r43_doctor_names_a_starving_node(tmp_path, monkeypatch, doctor):
    world = ClockWorld(tmp_path, monkeypatch, now=utc(2026, 10, 12, 8, 0),
                       scheduler={"starvation_after_seconds": 3600})
    try:
        world.start_scheduler()
        world.hold_lock("L")
        waiter = world.simple("W", locks=["L"])
        world.until(lambda: "lock" in world.codes(waiter), what="W blocked")
        world.set_clock(utc(2026, 10, 12, 10, 0))
        _, _, out = doctor(world)
        assert [ln for ln in out.splitlines() if waiter in ln and "starv" in ln.lower()], out
    finally:
        world.close()


def test_nc_r55_doctor_names_pending_nodes_when_the_gate_is_off(w, doctor):
    w.start_scheduler()
    pending = w.simple("P", fx={"gate": "gP"})
    w.wait_running(pending)
    w.stop_scheduler()
    _, base_on, _ = doctor(w)
    w.project["scheduler"]["enabled"] = False
    w.write_config()
    code, problems, out = doctor(w)
    assert pending in out, out
    assert [ln for ln in out.splitlines() if "!" in ln and pending in ln], out
    assert problems >= 1


# ================================================================== NC-R44

@pytest.fixture(scope="module")
def prompts(tmp_path_factory):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_c13_plans_notes as c13
    base = tmp_path_factory.mktemp("nc44")
    root = c13.init_project(base)
    out = {}
    for key, args in (("implement", ("orchestrator", "--team", "implement")),
                      ("review", ("orchestrator", "--team", "review"))):
        r = c13.run_cli(base, root, "prompt", *args)
        assert r.returncode == 0, r.stdout + r.stderr
        out[key] = r.stdout
    out["near"] = c13.near
    return out


KEYS = ["implement", "review"]


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_the_orchestrator_prompt_has_a_section_on_planning_with_nodes(prompts, key):
    t = prompts[key]
    heads = [ln for ln in t.splitlines() if ln.startswith("#") and re.search(r"\bnodes?\b", ln, re.I)]
    assert heads, "no heading names nodes"
    assert any(re.search(r"plan", h, re.I) for h in heads), heads


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("word", [
    "create_node", "wait_for_nodes", "ack_nodes", "instantiate_template", "merge_node",
    "relaunch_node", "loop_max", "get_node", "list_nodes"])
def test_nc_r44_the_section_names_each_tool_and_signal(prompts, key, word):
    assert word in prompts[key], word


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_it_says_to_deposit_ahead_and_to_use_the_shipped_templates(prompts, key):
    t, near = prompts[key], prompts["near"]
    assert near(t, "deposit", "ahead") or near(t, "deposit", "in advance") or near(t, "deposit", "before")
    assert "implement" in t and "review-loop" in t
    assert near(t, "review-loop", "template") and near(t, "implement", "template")


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_it_says_waiting_is_optional_and_acknowledgement_advances_the_cursor(prompts, key):
    t, near = prompts[key], prompts["near"]
    assert near(t, "wait_for_nodes", "ack_nodes")
    assert near(t, "ack_nodes", "cursor") or near(t, "ack_nodes", "acknowledg")
    assert near(t, "wait_for_nodes", "optional") or near(t, "wait_for_nodes", "not required") \
        or near(t, "wait_for_nodes", "nothing waits")


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_it_gives_the_loop_max_decision_to_the_orchestrator(prompts, key):
    t, near = prompts[key], prompts["near"]
    assert near(t, "loop_max", "relaunch_node")
    assert near(t, "loop_max", "opus") or near(t, "relaunch_node", "opus"), \
        "the 'round 3 -> opus' example of an orchestrator choice is missing"
    assert near(t, "loop_max", "max_rounds") or near(t, "relaunch_node", "max_rounds")
    assert near(t, "loop_max", "your", "decision") or near(t, "loop_max", "you decide") \
        or near(t, "loop_max", "orchestrator", "choice") or near(t, "loop_max", "decide")


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_merge_node_is_the_only_way_to_land_a_plan_on_main(prompts, key):
    t, near = prompts[key], prompts["near"]
    assert near(t, "merge_node", "main")
    assert near(t, "merge_node", "verdict", neg=True) or near(t, "merge_node", "approved") \
        or near(t, "merge_node", "done")


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_it_tells_the_orchestrator_run_tools_take_run_ids_not_node_ids(prompts, key):
    t, near = prompts[key], prompts["near"]
    assert near(t, "node_id", "get_node") or near(t, "node id", "get_node")


@pytest.mark.parametrize("key", KEYS)
def test_nc_r44_the_older_orchestrator_sections_are_still_there(prompts, key):
    t = prompts[key]
    for word in ("start_agent", "wait_for_agents", "merge_agent", "context/plans/"):
        assert word in t, word
    assert len(t) > 1000


def test_nc_r44_the_shipped_file_carries_the_section_too():
    from multiagents.paths import shipped_defaults_dir
    text = (shipped_defaults_dir() / "agents" / "team" / "_orchestrator.md").read_text()
    assert "wait_for_nodes" in text and "merge_node" in text and "loop_max" in text
