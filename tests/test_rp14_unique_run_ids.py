"""RP14 (finding F207): run ids are unique within a project.

Spec: context/specs/readonly-restore-provenance.md, RP14. Every run id that
multiagents allocates -- the scheduler's attempt launch (`engine.py`), the
session-alias cleanup attempt (`scheduler/sessions.py`) and `tree.new_id` for
every caller (a root session's `Runner.start`, the scheduler's
`admit_consult`) -- differs from every run id already known to the project:
in the run tree, in the scheduler's attempt records, and as a directory under
`.multiagents/runs/`. On a clash allocation draws again; it never reuses or
overwrites. The format stays `ag-` + 6 lowercase hex. Two allocations racing
never return the same id.

Black box: each test drives a public entry point (an engine tick, the
scheduler RPC, `Runner.start`, `sessions.request_cleanup`) and reads the
resulting run id from the records that entry point produces.

Assumed seam (the spec says "a random source forced to return ..."): run ids
are drawn from `uuid.uuid4()`, looked up on the `uuid` module at call time,
as all three sites do today. The forced source returns UUIDs whose first six
hex digits are the clashing id for the first `CLASHES` draws of the operation
and a fresh, recorded prefix afterwards; the rest of each UUID stays random,
so attempt and activation ids drawn from the same source stay distinct. The
fresh run id must be one of the fresh prefixes the source handed out ("the
fresh one is returned"), which also proves the allocation really went through
the forced source. `CLASHES` is a small number of draws: an allocator that
gives up after fewer consecutive clashes than that is not "drawing again".
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.scheduler import sessions  # noqa: E402
from multiagents.scheduler.engine import save_attempt  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402

RUN_ID = re.compile(r"ag-[0-9a-f]{6}")
CLASH = "c1a5e0"
CLASHES = 6
TICK_WAIT = 3          # seconds an engine tick may take to claim its nodes
RUN_WAIT = 10          # seconds a started fixture run may take to end
PROCESS_WAIT = 60      # seconds a child allocator process may take
PLACES = ["tree", "attempt", "rundir"]
REAL_UUID4 = uuid.uuid4


class ForcedDraws:
    """`uuid.uuid4` replacement: `clash` prefix for the first `clashes`
    draws, then fresh prefixes (recorded in `fresh`). Thread-safe."""

    def __init__(self, clash: str, clashes: int):
        self.clash = clash
        self.left = clashes
        self.fresh: list[str] = []
        self.lock = threading.Lock()

    def __call__(self) -> uuid.UUID:
        with self.lock:
            tail = REAL_UUID4().hex[6:]
            if self.left > 0:
                self.left -= 1
                return uuid.UUID(hex=self.clash + tail)
            prefix = self.clash
            while prefix == self.clash or prefix in self.fresh:
                prefix = REAL_UUID4().hex[:6]
            self.fresh.append(prefix)
            return uuid.UUID(hex=prefix + tail)


def force(monkeypatch, clash=CLASH, clashes=CLASHES) -> ForcedDraws:
    draws = ForcedDraws(clash, clashes)
    monkeypatch.setattr(uuid, "uuid4", draws)
    return draws


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


# ----------------------------------------------------------------- planting
def plant(h, where: str, run_id: str = "ag-" + CLASH):
    """Make `run_id` known to the project in one place; return a snapshot
    function of that place's record for the untouched check."""
    if where == "tree":
        h.engine.runner.tree.add(Node(id=run_id, agent="coder", provider="fx", model="fx/m1",
                                      parent=None, depth=1, task="earlier run", status="done"))
        return lambda: h.engine.runner.tree.read()["nodes"].get(run_id)
    if where == "attempt":
        owner = h.record()
        owner.update(state="done", outcome="completed")
        h.save(owner)
        record = {"attempt_id": REAL_UUID4().hex, "activation_id": REAL_UUID4().hex,
                  "node_id": owner["id"], "run_id": run_id, "state": "abandoned",
                  "locks": [], "at": now(), "ended_at": now()}
        with h.service.store.transaction() as db:
            save_attempt(db, record)
        return lambda: [a for a in h.journal().values() if a["run_id"] == run_id]
    if where == "rundir":
        run_dir = h.world.paths.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stream.jsonl").write_text('{"earlier": true}\n')
        (run_dir / "result.json").write_text('{"status": "done"}\n')

        def snapshot():
            return sorted((str(p.relative_to(run_dir)), p.read_bytes())
                          for p in run_dir.rglob("*") if p.is_file())
        return snapshot
    raise AssertionError(where)


def assert_fresh(run_id, draws: ForcedDraws, clash=CLASH):
    assert isinstance(run_id, str) and RUN_ID.fullmatch(run_id), \
        f"run id {run_id!r} is not ag- + 6 lowercase hex"
    assert run_id != "ag-" + clash, f"the clashing id {run_id} was allocated again"
    assert run_id[3:] in draws.fresh, (
        f"run id {run_id} is not one of the fresh draws {draws.fresh} "
        "(it did not come from drawing again)")


# ------------------------------------------------------------ engine launch
def tick_claims(h, monkeypatch, expect):
    """Run one engine tick with the admission probe admitting and each spawn
    recorded instead of launched; the tick ends once every node in `expect`
    has been spawned (or after TICK_WAIT seconds)."""
    async def start(*args, **kwargs):
        return {"admitted": True}

    spawned = []

    def spawn(attempt):
        spawned.append(attempt)
        # What the real launch does first: reserve the run in the tree, which
        # the tick waits for before the next candidate. Never overwrite.
        tree = h.engine.runner.tree
        if tree.get(attempt["run_id"]) is None:
            tree.add(Node(id=attempt["run_id"], agent="coder", provider="fx", model="fx/m1",
                          parent=None, depth=1, task="spawned", status="running"))
        if set(expect) <= {a["node_id"] for a in spawned}:
            h.engine.stopped.set()

    monkeypatch.setattr(h.engine.runner, "start", start)
    monkeypatch.setattr(h.engine, "spawn", spawn)
    timer = threading.Timer(TICK_WAIT, h.engine.stopped.set)
    timer.start()
    try:
        asyncio.run(h.engine.tick())
    finally:
        timer.cancel()
    return spawned


def claimed_run(h, node_id):
    runs = [a["run_id"] for a in h.journal().values() if a["node_id"] == node_id]
    assert len(runs) == 1, f"expected one attempt for {node_id}, got {runs}"
    return runs[0]


@pytest.mark.parametrize("where", PLACES)
def test_rp14_engine_attempt_launch_draws_again_on_a_known_id(h, monkeypatch, where):
    snapshot = plant(h, where)
    before = snapshot()
    node = h.record()
    h.save(node)
    draws = force(monkeypatch)
    tick_claims(h, monkeypatch, [node["id"]])
    run_id = claimed_run(h, node["id"])
    assert_fresh(run_id, draws)
    assert snapshot() == before, f"the earlier run's {where} record was touched"


def test_rp14_engine_two_launches_in_one_tick_on_the_same_draw_get_different_ids(h, monkeypatch):
    first, second = h.record(), h.record()
    h.save(first, second)
    draws = force(monkeypatch, clash="d0d0d0", clashes=2 * CLASHES)
    tick_claims(h, monkeypatch, [first["id"], second["id"]])
    ids = [claimed_run(h, first["id"]), claimed_run(h, second["id"])]
    assert all(RUN_ID.fullmatch(i) for i in ids), ids
    assert ids[0] != ids[1], f"two attempts were given the same run id {ids[0]}"
    assert all(i[3:] in {"d0d0d0", *draws.fresh} for i in ids), \
        f"{ids} did not come from the forced source"


# ----------------------------------------------------- session-alias cleanup
def cleanup_setup(h):
    """A node and an alias binding whose last run resumed a session, as
    TB2-R6's cleanup reservation requires; returns (node, binding)."""
    previous_id = "ag-" + REAL_UUID4().hex[:6]
    h.engine.runner.tree.add(Node(id=previous_id, agent="coder", provider="fx", model="fx/m1",
                                  parent=None, depth=1, status="done", session_id="sess-1",
                                  branch="agents/coder/" + previous_id[3:]))
    node = h.record()
    h.save(node)
    binding = {"id": "alias-" + REAL_UUID4().hex[:6], "last_run": previous_id,
               "commit": h.world.main_tip(), "account": "fx", "worktree": str(h.world.root),
               "home_id": "home-1"}
    return node, binding


def request(h, node, binding):
    """Persist the cleanup reservation for `node` (`sessions.request_cleanup`)."""
    with h.service.store.transaction() as db:
        current = h.service.store.nodes(db)[node["id"]]
        result = sessions.request_cleanup(h.service.store, db, current, binding, h.engine.runner,
                                          "/nonexistent/archive.tar", "clean the worktree")
    return result


def cleanup_run(h, node_id):
    runs = [a["run_id"] for a in h.journal().values()
            if a["node_id"] == node_id and a.get("cleanup")]
    assert len(runs) == 1, f"expected one cleanup attempt for {node_id}, got {runs}"
    return runs[0]


@pytest.mark.parametrize("where", PLACES)
def test_rp14_session_cleanup_attempt_draws_again_on_a_known_id(h, monkeypatch, where):
    snapshot = plant(h, where)
    before = snapshot()
    node, binding = cleanup_setup(h)
    draws = force(monkeypatch)
    request(h, node, binding)
    run_id = cleanup_run(h, node["id"])
    assert_fresh(run_id, draws)
    assert snapshot() == before, f"the earlier run's {where} record was touched"


def test_rp14_session_cleanup_and_engine_launch_on_the_same_draw_get_different_ids(h, monkeypatch):
    """The cleanup reservation allocates, then the next tick launches another
    node, both forced onto the same draw."""
    node, binding = cleanup_setup(h)
    other = h.record()
    h.save(other)
    force(monkeypatch, clash="d1d1d1", clashes=2 * CLASHES)
    request(h, node, binding)
    first = cleanup_run(h, node["id"])
    tick_claims(h, monkeypatch, [other["id"]])
    second = claimed_run(h, other["id"])
    assert RUN_ID.fullmatch(first) and RUN_ID.fullmatch(second), (first, second)
    assert first != second, f"the cleanup and the launch share the run id {first}"


# ---------------------------------------------- tree.new_id: Runner.start
@pytest.fixture
def hs(tmp_path, monkeypatch):
    """A harness whose project has the scheduler gate off, so a root session
    starts agents itself (the scheduler's records stay in the project)."""
    harness = Harness(tmp_path, monkeypatch)
    harness.world.project["scheduler"]["enabled"] = False
    harness.world.write_config()
    # Quota readings are not under test; the real ones shell out per provider.
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {})
    yield harness
    harness.close()


def root_runner(h):
    return Runner(h.world.paths, h.service.configuration())


def start_one(runner, task="work"):
    async def go():
        started = await runner.start("coder", task)
        assert "agent_id" in started, started
        agent = started["agent_id"]
        run = runner.runs.get(agent)
        if run is not None and run.task is not None:
            await asyncio.wait_for(run.task, timeout=RUN_WAIT)
        return agent
    return asyncio.run(go())


@pytest.mark.parametrize("where", PLACES)
def test_rp14_start_agent_draws_again_on_a_known_id(hs, monkeypatch, where):
    snapshot = plant(hs, where)
    before = snapshot()
    runner = root_runner(hs)
    draws = force(monkeypatch)
    run_id = start_one(runner)
    assert_fresh(run_id, draws)
    assert snapshot() == before, f"the earlier run's {where} record was touched"
    assert runner.tree.get(run_id) is not None and runner.tree.get(run_id).task == "work"


def test_rp14_two_concurrent_start_agents_on_the_same_draw_get_different_ids(hs, monkeypatch):
    force(monkeypatch, clash="d2d2d2", clashes=2 * CLASHES)
    runners = [root_runner(hs), root_runner(hs)]
    barrier = threading.Barrier(2)
    ids: list = [None, None]
    errors: list = []

    def worker(i):
        try:
            barrier.wait(timeout=RUN_WAIT)
            ids[i] = start_one(runners[i], task=f"task-{i}")
        except BaseException as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2 * RUN_WAIT)
    assert not errors, errors
    assert all(isinstance(i, str) and RUN_ID.fullmatch(i) for i in ids), ids
    assert ids[0] != ids[1], f"two concurrent starts were given the same run id {ids[0]}"
    tasks = {runners[0].tree.get(i).task for i in ids}
    assert tasks == {"task-0", "task-1"}, f"one run's record overwrote the other's: {tasks}"


CHILD = r"""
import asyncio, json, sys, threading, uuid
sys.path.insert(0, sys.argv[1])
from multiagents import budget
budget.read_all = lambda *a, **k: {}   # a separate process: quota readings are not under test
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from pathlib import Path

real = uuid.uuid4
left = [int(sys.argv[4])]
def forced():
    tail = real().hex[6:]
    if left[0] > 0:
        left[0] -= 1
        return uuid.UUID(hex=sys.argv[3] + tail)
    return real()
uuid.uuid4 = forced          # a separate process: nothing else shares it
paths = ProjectPaths(Path(sys.argv[2]))
runner = Runner(paths, load(paths, seed=False))
gate = Path(sys.argv[5])
while not gate.exists():
    pass
async def go():
    started = await runner.start("coder", sys.argv[6])
    run = runner.runs.get(started.get("agent_id"))
    if run is not None and run.task is not None:
        await asyncio.wait_for(run.task, timeout=10)
    return started
print(json.dumps(asyncio.run(go())))
"""


def test_rp14_two_processes_allocating_on_the_same_draw_get_different_ids(hs, tmp_path):
    """The scheduler process and root sessions allocate in separate processes
    of one project, so the guarantee must hold across processes."""
    src = str(Path(__file__).resolve().parent.parent / "src")
    gate = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=src)
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, src, str(hs.world.root), "d3d3d3",
                               str(CLASHES), str(gate), f"task-{i}"],
                              cwd=hs.world.root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for i in range(2)]
    try:
        gate.write_text("")
        outs = [p.communicate(timeout=PROCESS_WAIT) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    for p, (out, err) in zip(procs, outs):
        assert p.returncode == 0, err[-3000:]
    ids = [json.loads(out.strip().splitlines()[-1]).get("agent_id") for out, _ in outs]
    assert all(isinstance(i, str) and RUN_ID.fullmatch(i) for i in ids), ids
    assert ids[0] != ids[1], f"two processes were given the same run id {ids[0]}"
    nodes = root_runner(hs).tree.read()["nodes"]
    assert {nodes[i]["task"] for i in ids} == {"task-0", "task-1"}


# ----------------------------------------- tree.new_id: scheduler admit_consult
def admit(h):
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    reply = h.service.request({"op": "admit_consult", "token": token,
                               "args": {"agent": "advisor"},
                               "request_id": "ac-" + REAL_UUID4().hex})
    assert reply.get("ok"), reply
    result = reply["result"]
    return result.get("consult_run_id") if isinstance(result, dict) else None


@pytest.fixture
def hc(tmp_path, monkeypatch):
    """A harness whose config has a conversational `advisor`."""
    harness = Harness(tmp_path, monkeypatch)
    harness.world.agent("advisor", "fx", conversational=True)
    harness.world.write_config()
    yield harness
    harness.close()


@pytest.mark.parametrize("where", PLACES)
def test_rp14_consult_admission_draws_again_on_a_known_id(hc, monkeypatch, where):
    snapshot = plant(hc, where)
    before = snapshot()
    draws = force(monkeypatch)
    run_id = admit(hc)
    assert_fresh(run_id, draws)
    assert snapshot() == before, f"the earlier run's {where} record was touched"


def test_rp14_two_concurrent_consult_admissions_on_the_same_draw_get_different_ids(hc, monkeypatch):
    force(monkeypatch, clash="d4d4d4", clashes=2 * CLASHES)
    barrier = threading.Barrier(2)
    ids: list = [None, None]
    errors: list = []

    def worker(i):
        try:
            barrier.wait(timeout=RUN_WAIT)
            ids[i] = admit(hc)
        except BaseException as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2 * RUN_WAIT)
    assert not errors, errors
    assert all(isinstance(i, str) and RUN_ID.fullmatch(i) for i in ids), ids
    assert ids[0] != ids[1], f"two concurrent admissions were given the same run id {ids[0]}"
