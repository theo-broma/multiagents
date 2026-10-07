"""TB2-R7: every hold that waits for the orchestrator is signalled to it.

Contract: `context/specs/tooling-batch-2-2026-10.md` § TB2-R7.

Three public surfaces, each driven the way its consumer drives it:

- the injected message: `driver._supervise` (what `multiagents run
  --unattended` runs) against a real scheduler process. Every turn is a real
  subprocess, a fake provider script reached through `scripts.exec_action`; the
  fake logs what each turn was launched with (argv and `MULTIAGENTS_*` env,
  which is where the driver puts the message it injects into the session). A
  "signal" below is a launch whose argv/env carries a node id.
- the waits: the MCP tools `server.wait_for_agents` / `server.wait_for_nodes`
  called in-process as the orchestrator does (`call_tool`).
- the file: `.multiagents/attention.json`, read from disk.

Holds are produced by real node plans on the fixture providers:
`input_conflict` (a pre-launch hold, the node never has a run), `loop_max`,
`needs_info` and a session alias's `dirty_worktree`.

Where the contract is silent (kept loose, see also the Result of the run):
- the shape of `attention.json` is not pinned: it must parse as JSON and name
  the nodes (and their reasons) it lists; whether it is a list or a dict, and
  its per-hold fields, are free.
- the wording of the injected message is not pinned: it must carry the node id,
  the reason, the detail (the dirty-diff path, the NEED_INFO question) and name
  at least one tool of the root's vocabulary as the expected action.
- how a wait reports a hold is not pinned beyond "the node id and the reason
  are in the reply, it did not time out".
- the attached (interactive) driver path has no seam a black-box test can
  observe; only the unattended supervisor is driven here.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents import driver  # noqa: E402
from multiagents.config import AgentSpec, Config  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now  # noqa: E402
from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402
from nc_fixture.world import call_tool, unwrap  # noqa: E402

WAIT = 8             # one launch / one transition / one hold appearing
ATTN = 3             # attention.json is written when the hold is
WAIT_ROUNDS = 20     # several activations in a row
TURN_BOUND = 25      # a whole supervised run of a few fake turns
CALL_TIMEOUT = 4     # `timeout` given to a wait that must NOT need it
WATCH = 6            # `timeout` of a wait that a hold must cut short
FAST = 3.5           # a wait that "returns at once" must be back inside this
SESSION = "c0ffee00-0000-4000-8000-0000000007c7"
NODE_ID = re.compile(r"\bnd-[0-9a-f]{8}\b")
ACTIONS = ("relaunch_node", "close_node", "answer_question", "update_node")

PAIR = """\
template: r7pair
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: wrk, kind: simple, agent: wk, task: "implement the thing"}
    - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
"""

FAKE_TURN = r'''#!{python}
import json, os, pathlib, sys, time
action = sys.argv[1] if len(sys.argv) > 1 else "check"
log = pathlib.Path(os.environ["FAKE_LOG"])
ctl_path = pathlib.Path(os.environ["FAKE_CTL"])
ctl = json.loads(ctl_path.read_text()) if ctl_path.is_file() else {}


def put(record):
    with log.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


if action == "check":
    print("logged in"); sys.exit(0)
if action == "prepare":
    sys.exit(0)
if action == "compact":
    sys.exit(0)
if action != "launch":
    sys.exit(64)
prior = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.is_file() else []
n = 1 + sum(1 for e in prior if e["event"] == "start")
put({"event": "start", "n": n, "t": time.time(), "argv": sys.argv[1:],
     "env": {k: v for k, v in os.environ.items() if k.startswith("MULTIAGENTS_")}})
if ctl.get("block_first") and n == 1:
    release = pathlib.Path(ctl["release"])
    deadline = time.time() + 30
    while not release.exists() and time.time() < deadline:
        time.sleep(0.05)
with open(ctl["events"], "a") as fh:      # activity: the supervisor keeps going
    fh.write(json.dumps({"t": time.time(), "agent": "fake", "kind": "fake_activity", "turn": n}) + "\n")
put({"event": "end", "n": n, "t": time.time()})
sys.exit(0)
'''


# ------------------------------------------------------------------ world

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    world.start_scheduler()
    yield world
    world.close()


def attention_path(w) -> Path:
    return w.root / ".multiagents" / "attention.json"


def attention_text(w) -> str:
    path = attention_path(w)
    if not path.is_file():
        return ""
    text = path.read_text()
    json.loads(text)                      # whatever its shape, it is JSON
    return text


def hold_of(w, node_id: str) -> dict:
    node = w.get(node_id)
    assert node["state"] == "held", node
    return node["hold"]


def conflicting_inputs(w) -> list[str]:
    """Two finished nodes whose branches cannot be combined."""
    w.fxw.queue(commit_entry("shared.txt", "from A\n", "a"),
                commit_entry("shared.txt", "from B\n", "b"))
    a = w.simple("A", "wk")
    w.wait_state(a, "done", timeout=WAIT)
    b = w.simple("B", "wk")
    w.wait_state(b, "done", timeout=WAIT)
    return [a, b]


def input_conflict(w, tag: str = "C", ins: list[str] | None = None, **fields) -> str:
    """A node that is held before it ever launches: it has no run."""
    a, b = ins or conflicting_inputs(w)
    node = w.simple(tag, "wk", inputs=[{"node": a}, {"node": b}], **fields)
    w.wait_held(node, "input_conflict", timeout=WAIT)
    assert w.get(node)["runs"] == [], "the node was launched: not a pre-launch hold"
    return node


def group(w, children: list[str]) -> str:
    reply = w.create({"kind": "group", "children": children})
    assert reply.get("ok"), reply
    return unwrap(reply["result"])["id"]


def loop_max(w, rounds: int = 1) -> tuple[str, str, str]:
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("rejected", [finding("broken")]))
    loop, wk, rv = w.mkloop(rounds)
    w.wait_held(loop, "loop_max", timeout=WAIT_ROUNDS)
    return loop, wk, rv


def dirty_worktree(w) -> str:
    w.register_ok(PAIR)
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], leave={"scratch.txt": "left behind\n"}),
                verdict_entry("approved"))
    top = w.instantiate_ok("r7pair", {"rounds": 3})
    _, rv = w.children(top)
    w.wait_held(rv, "dirty_worktree", timeout=WAIT_ROUNDS)
    return rv


def needs_info(w, question: str = "WHICH-DB-7c7 should the migration use?") -> str:
    node = w.simple("N", "wk", fx={"text": f"NEED_INFO(db): {question}"})
    w.wait_held(node, "needs_info", timeout=WAIT)
    return node


# ----------------------------------------------------------------- driver

class Orchestrator:
    """`driver._supervise` over the world's project with a logging fake provider."""

    def __init__(self, w: M4World, tmp_path: Path, monkeypatch):
        self.w = w
        self.paths = w.paths
        self.provider_log = tmp_path / "turns.log"
        self.ctl = tmp_path / "turns.ctl.json"
        self.release = tmp_path / "release"
        script = self.paths.config / "providers" / "r7turn.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(FAKE_TURN.replace("{python}", sys.executable))
        script.chmod(0o755)
        data = {"bin": "true", "script": "r7turn.py", "spawn": {"args": ["x"]},
                "env": {"FAKE_LOG": str(self.provider_log), "FAKE_CTL": str(self.ctl)}}
        monkeypatch.setenv("FAKE_LOG", str(self.provider_log))
        monkeypatch.setenv("FAKE_CTL", str(self.ctl))
        monkeypatch.chdir(w.root)
        self.provider = Provider.from_dict("r7prov", data)
        self.config = Config(
            project={"scheduler": {"enabled": True, "tick_seconds": 1}, "limits": {}},
            providers={"r7prov": data}, agents={}, models={}, instruction_dirs=[])
        self.spec = AgentSpec("orchestrator", "r7prov", "m")
        self.context = {"MULTIAGENTS_RESUME": "0", "MULTIAGENTS_SESSION_ID": SESSION,
                        "MULTIAGENTS_ROLE": "orchestrator",
                        "MULTIAGENTS_PROJECT": str(w.root), "MULTIAGENTS_MODEL": "m"}
        Tree(self.paths.tree_file, self.paths.events_file).add(
            Node(id="dr-c0ffee", agent="orchestrator", provider="r7prov", model="m",
                 parent=None, depth=0, status="running", task="orchestrator session",
                 session=SESSION, session_id=SESSION, role="orchestrator",
                 started_at=tree_now()))
        fast = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time)
                                        if not n.startswith("__")})
        fast.sleep = lambda seconds: None           # the failed-turn backoff only
        monkeypatch.setattr(driver, "time", fast)
        self.thread: threading.Thread | None = None
        self.code: list[int] = []

    def control(self, **ctl) -> None:
        self.ctl.write_text(json.dumps({"events": str(self.paths.events_file),
                                        "release": str(self.release), **ctl}))

    def _run(self, turns: int) -> None:
        self.code.append(driver._supervise(self.paths, self.config, "orchestrator", self.spec,
                                           self.provider, object(), dict(self.context), turns))

    def run(self, turns: int, **ctl) -> None:
        self.control(**ctl)
        self._run(turns)

    def start(self, turns: int, **ctl) -> None:
        self.control(**ctl)
        self.thread = threading.Thread(target=self._run, args=(turns,), daemon=True)
        self.thread.start()

    def finish(self, timeout: float = TURN_BOUND) -> None:
        self.release.write_text("go")
        if self.thread is not None:
            self.thread.join(timeout)
            assert not self.thread.is_alive(), "the supervisor did not finish"

    def entries(self, event: str | None = None) -> list[dict]:
        if not self.provider_log.is_file():
            return []
        out = [json.loads(line) for line in self.provider_log.read_text().splitlines() if line.strip()]
        return [e for e in out if event is None or e["event"] == event]

    def starts(self) -> list[dict]:
        return self.entries("start")

    def text_of(self, start: dict) -> str:
        """What the turn was launched with, minus the standing nudge."""
        env = {k: v for k, v in start["env"].items() if k != "MULTIAGENTS_PROJECT"}
        return (" ".join(start["argv"]) + "\n" + "\n".join(f"{k}={v}" for k, v in env.items())
                ).replace(driver.NUDGE, "")

    def signals(self) -> list[str]:
        """The text of every launch that carries a node id: one per message."""
        return [t for t in (self.text_of(s) for s in self.starts()) if NODE_ID.search(t)]


@pytest.fixture
def orch(w, tmp_path, monkeypatch):
    return Orchestrator(w, tmp_path, monkeypatch)


def mentions(text: str, node_id: str, reason: str) -> bool:
    return node_id in text and reason in text


# ===================================================== the injected message

def test_tb2_r7_a_pre_launch_dirty_worktree_hold_is_injected_exactly_once(w, orch):
    """Verified-by 1: dirty_worktree while the orchestrator is idle, one message,
    however many turns follow."""
    node = dirty_worktree(w)
    detail = hold_of(w, node)["detail"]
    assert detail, "the fixture hold carries no detail to look for"
    orch.run(4, activity=True)
    assert len(orch.starts()) >= 3, "the supervisor stopped before the repetition could show"
    signals = orch.signals()
    assert len(signals) == 1, f"expected exactly one injected message, got {len(signals)}: {signals}"
    text = signals[0]
    assert node in text and "dirty_worktree" in text
    assert Path(detail).name in text, "the detail (the dirty-diff path) is not in the message"
    assert any(a in text for a in ACTIONS), "the message names no expected action"


@pytest.mark.parametrize("make,reason,marker", [
    (lambda w: input_conflict(w), "input_conflict", None),
    (lambda w: loop_max(w)[0], "loop_max", None),
    (lambda w: needs_info(w), "needs_info", "WHICH-DB-7c7"),
], ids=["input_conflict", "loop_max", "needs_info"])
def test_tb2_r7_every_reason_that_needs_a_root_action_is_signalled(w, orch, make, reason, marker):
    node = make(w)
    orch.run(3, activity=True)
    signals = orch.signals()
    assert len(signals) == 1, signals
    assert mentions(signals[0], node, reason), signals[0]
    assert any(a in signals[0] for a in ACTIONS), signals[0]
    if marker:
        assert marker in signals[0], "the hold's detail (the question) is not in the message"


def test_tb2_r7_holds_arriving_together_are_one_message(w, orch):
    ins = conflicting_inputs(w)
    first = input_conflict(w, "C1", ins)
    second = input_conflict(w, "C2", ins)
    orch.run(4, activity=True)
    signals = orch.signals()
    assert len(signals) == 1, f"two holds, {len(signals)} messages: {signals}"
    assert first in signals[0] and second in signals[0]
    assert signals[0].count("input_conflict") >= 2, "each hold's reason must be named"


def test_tb2_r7_a_leaf_hold_under_two_nested_composites_is_one_signal_not_three(w, orch):
    leaf = input_conflict(w)
    inner = group(w, [leaf])
    outer = group(w, [inner])
    w.until(lambda: w.get(outer)["state"] == "held", timeout=WAIT, what="the ancestors to be held")
    orch.run(4, activity=True)
    signals = orch.signals()
    assert len(signals) == 1, signals
    text = signals[0]
    assert leaf in text and "input_conflict" in text
    assert "child_held" not in text, "an ancestor's child_held was signalled"
    assert inner not in text and outer not in text, "an ancestor was named as if it were a hold"


def test_tb2_r7_a_hold_that_arrives_mid_turn_is_delivered_when_the_turn_ends(w, orch):
    ins = conflicting_inputs(w)
    orch.start(5, block_first=True, activity=True)
    w.until(lambda: orch.starts(), timeout=WAIT, what="the first turn to start")
    node = input_conflict(w, "C", ins)               # the hold appears while turn 1 runs
    w.quiet(1.5)                                      # a negative: nothing may come in meanwhile
    assert len(orch.starts()) == 1, "a second launch overlapped the running turn"
    assert orch.entries("end") == [], "the turn was cut short"
    assert not orch.signals(), "the message was injected into a turn in progress"
    orch.finish()
    entries = orch.entries()
    ended = next(i for i, e in enumerate(entries) if e["event"] == "end" and e["n"] == 1)
    signalled = [i for i, e in enumerate(entries)
                 if e["event"] == "start" and NODE_ID.search(orch.text_of(e))]
    assert len(signalled) == 1, orch.signals()
    assert signalled[0] > ended, "the message was delivered before the running turn ended"
    assert mentions(orch.signals()[0], node, "input_conflict")


def test_tb2_r7_blockers_that_clear_themselves_are_not_signalled(w, orch):
    """session_busy / dependency / lock / window are `blocked`, not holds."""
    w.fxw.queue({"gate": "ga", "commit": "a", "write": {"a.txt": "a\n"}})
    running = w.simple("A", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)
    dependent = w.simple("D", "wk", depends_on=[{"node": running}])
    locked = w.simple("L", "wk", locks=["R7"], fx={"gate": "gl"})
    w.wait_running(locked, timeout=WAIT)
    waiting_lock = w.simple("L2", "wk", locks=["R7"])
    try:
        orch.run(3, activity=True)
        assert orch.signals() == [], "a self-clearing block was injected as if it were a hold"
        assert w.get(dependent)["state"] == "open" and w.get(waiting_lock)["state"] == "open"
    finally:
        w.gate("ga")
        w.gate("gl")


def test_tb2_r7_no_holds_means_no_message(w, orch):
    node = w.simple("A", "wk")
    w.wait_state(node, "done", timeout=WAIT)
    orch.run(3, activity=True)
    assert orch.signals() == []


# ================================================================== the waits

def test_tb2_r7_wait_for_agents_on_a_node_with_no_run_returns_on_its_hold(w):
    """Verified-by 2: today this answers `no_run_yet` and says nothing of the hold."""
    node = input_conflict(w)
    start = time.monotonic()
    reply = call_tool(w, "wait_for_agents", [node], timeout=CALL_TIMEOUT)
    assert time.monotonic() - start < FAST
    text = json.dumps(reply)
    assert reply.get("timed_out") is not True, reply
    assert node in text and "input_conflict" in text, f"the hold is not in the reply: {reply}"


def test_tb2_r7_wait_for_agents_without_ids_returns_on_an_unacknowledged_hold(w):
    ins = conflicting_inputs(w)
    running = w.simple("G", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)             # so the wait has something to wait on
    node = input_conflict(w, "C", ins)
    try:
        start = time.monotonic()
        reply = call_tool(w, "wait_for_agents", None, timeout=CALL_TIMEOUT)
        assert time.monotonic() - start < FAST, "the wait sat on a running node and ignored the hold"
        assert reply.get("timed_out") is not True, reply
        assert node in json.dumps(reply) and "input_conflict" in json.dumps(reply), reply
    finally:
        w.gate("ga")


def test_tb2_r7_wait_for_agents_without_ids_wakes_when_a_hold_arrives_during_the_wait(w):
    ins = conflicting_inputs(w)
    running = w.simple("G", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)
    box: dict = {}

    def waiter():
        box["start"] = time.monotonic()
        box["reply"] = call_tool(w, "wait_for_agents", None, timeout=WATCH)
        box["end"] = time.monotonic()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    try:
        node = input_conflict(w, "C", ins)
        thread.join(WATCH + 2)
        assert not thread.is_alive(), "the wait did not return on the hold that arrived"
        reply = box["reply"]
        assert reply.get("timed_out") is not True, reply
        assert node in json.dumps(reply) and "input_conflict" in json.dumps(reply), reply
    finally:
        w.gate("ga")
        thread.join(2 * WAIT)


def test_tb2_r7_wait_for_agents_with_ids_returns_on_a_hold_of_another_owned_node(w):
    ins = conflicting_inputs(w)
    running = w.simple("G", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)
    run_id = w.get(running)["runs"][-1]["run_id"]
    node = input_conflict(w, "C", ins)
    try:
        start = time.monotonic()
        reply = call_tool(w, "wait_for_agents", [run_id], timeout=CALL_TIMEOUT)
        assert time.monotonic() - start < FAST, "waiting on one run ignored a hold elsewhere"
        assert reply.get("timed_out") is not True, reply
        assert node in json.dumps(reply) and "input_conflict" in json.dumps(reply), reply
    finally:
        w.gate("ga")


def test_tb2_r7_wait_for_agents_does_not_report_an_acknowledged_hold(w):
    node = input_conflict(w)
    cursor = call_tool(w, "wait_for_nodes", timeout=0)["next_cursor"]
    w.ok("ack_nodes", {"cursor": cursor})
    reply = call_tool(w, "wait_for_agents", None, timeout=1)
    assert not (node in json.dumps(reply) and "input_conflict" in json.dumps(reply)), (
        f"an acknowledged hold woke the wait again: {reply}")


def test_tb2_r7_wait_for_nodes_returns_at_once_on_an_unacknowledged_hold(w):
    node = input_conflict(w)
    start = time.monotonic()
    reply = call_tool(w, "wait_for_nodes", timeout=CALL_TIMEOUT)
    assert time.monotonic() - start < FAST
    held = [t for t in reply["transitions"] if t["node_id"] == node
            and t["kind"].removeprefix("node.") == "held"]
    assert len(held) == 1, reply["transitions"]


def test_tb2_r7_wait_for_nodes_wakes_on_a_hold_that_arrives_during_the_wait(w):
    ins = conflicting_inputs(w)
    cursor = call_tool(w, "wait_for_nodes", timeout=0)["next_cursor"]
    box: dict = {"seen": set()}

    def waiter():
        # follow the journal as the orchestrator does until the hold shows up
        position = cursor
        deadline = time.monotonic() + WATCH
        while time.monotonic() < deadline and not any(k == "held" for _, k in box["seen"]):
            reply = call_tool(w, "wait_for_nodes", cursor=position,
                              timeout=max(0.1, deadline - time.monotonic()))
            position = reply["next_cursor"]
            box["seen"] |= {(t["node_id"], t["kind"].removeprefix("node.")) for t in reply["transitions"]}
        box["end"] = time.monotonic()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    start = time.monotonic()
    node = input_conflict(w, "C", ins)
    thread.join(WATCH + 2)
    assert not thread.is_alive()
    assert (node, "held") in box["seen"], box["seen"]
    assert box["end"] - start < WATCH - 0.5, "the held transition only arrived with the timeout"


def test_tb2_r7_wait_for_nodes_after_ack_does_not_return_the_hold_again(w):
    node = input_conflict(w)
    first = call_tool(w, "wait_for_nodes", timeout=CALL_TIMEOUT)
    w.ok("ack_nodes", {"cursor": first["next_cursor"]})
    again = call_tool(w, "wait_for_nodes", timeout=0)
    assert all(t["node_id"] != node for t in again["transitions"]), again["transitions"]


# ======================================================= .multiagents/attention.json

def test_tb2_r7_attention_json_is_empty_of_nodes_when_nothing_is_held(w):
    node = w.simple("A", "wk")
    w.wait_state(node, "done", timeout=WAIT)
    assert not NODE_ID.search(attention_text(w))


def test_tb2_r7_attention_json_lists_the_hold_then_drops_it_after_relaunch_node(w):
    """Verified-by 4, with a hold that relaunch_node really resolves."""
    loop, wk, rv = loop_max(w)
    w.until(lambda: loop in attention_text(w), timeout=ATTN, what="attention.json to list the hold")
    assert "loop_max" in attention_text(w)
    w.fxw.queue(commit_entry("f2.txt", "v2\n", "round 2"))
    w.fxr.queue(verdict_entry("approved"))
    assert w.root_op("relaunch_node", loop, max_rounds=3).get("ok") is True
    w.until(lambda: loop not in attention_text(w), timeout=ATTN,
            what="attention.json to drop the hold after relaunch_node")
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert loop not in attention_text(w)


def test_tb2_r7_attention_json_drops_a_hold_closed_by_the_root(w):
    node = input_conflict(w)
    w.until(lambda: node in attention_text(w), timeout=ATTN, what="attention.json to list the hold")
    assert w.root_op("close_node", node, outcome="failed").get("ok") is True
    w.until(lambda: node not in attention_text(w), timeout=ATTN,
            what="attention.json to drop the closed hold")


def test_tb2_r7_attention_json_lists_every_current_hold_and_only_the_clearing_one_leaves(w):
    ins = conflicting_inputs(w)
    first = input_conflict(w, "C1", ins)
    second = input_conflict(w, "C2", ins)
    w.until(lambda: first in attention_text(w) and second in attention_text(w),
            timeout=ATTN, what="both holds listed")
    assert w.root_op("close_node", first, outcome="failed").get("ok") is True
    w.until(lambda: first not in attention_text(w), timeout=ATTN, what="the closed hold dropped")
    assert second in attention_text(w) and "input_conflict" in attention_text(w)


def test_tb2_r7_attention_json_lists_the_leaf_not_its_ancestors(w):
    leaf = input_conflict(w)
    inner = group(w, [leaf])
    outer = group(w, [inner])
    w.until(lambda: w.get(outer)["state"] == "held", timeout=WAIT, what="ancestors held")
    w.until(lambda: leaf in attention_text(w), timeout=ATTN, what="the leaf listed")
    text = attention_text(w)
    assert inner not in text and outer not in text, "an ancestor's child_held is not a hold of its own"
    assert "child_held" not in text


def test_tb2_r7_attention_json_does_not_list_blocks_that_clear_themselves(w):
    running = w.simple("A", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)
    dependent = w.simple("D", "wk", depends_on=[{"node": running}])
    held = input_conflict(w)
    w.until(lambda: held in attention_text(w), timeout=ATTN, what="the real hold listed")
    try:
        assert dependent not in attention_text(w)
    finally:
        w.gate("ga")
