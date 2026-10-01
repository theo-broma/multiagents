"""Adversary tests for the D1 limit-notice fixes (942df35).

Contract: `context/specs/d1-limit-notices-contract.md` (LN-C2..C5), and the
properties the fixes themselves claim in `notices.py` / `occupancy.py`:
the host-owned notice state is the only authority, `tree.json` is a display
mirror a container agent can write, a corrupt state fails safe, and
occupancy is judged over the run's whole life.

Every test here was red against 942df35 when written.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import notices  # noqa: E402
from multiagents.config import load  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.monitor.snapshot import snapshot  # noqa: E402
from multiagents.runner import Runner  # noqa: E402

from test_d1_limit_notices import _events, _project, _try_start  # noqa: E402
from test_d1_review_findings import (  # noqa: E402
    _docker_project, _hits_for, _node,
)


def _hit(tree, key="limits.max_concurrent", scope="tree"):
    return notices.hit(tree, key=key, value=1, effect="refused", scope=scope,
                       source=None, message=f"limit: {key} = 1 — refused.")


# ---------------------------------------------------------------------------
# A1 — LN-C5: a sibling that lived during the run and ended before the kill
# is still a sibling. Occupancy is asked only at the moment of the SIGKILL.


def test_adv_oom_sibling_that_ended_before_the_kill_is_still_a_sibling(tmp_path, monkeypatch):
    """LN-C5: `killed` only when this was the only run "alive during the
    interval"; "an increase during a run that had concurrent siblings" is
    `kill_uncertain`. Here the counter rises while a sibling is alive; the
    sibling then exits cleanly, and only afterwards is the worker SIGKILLed."""
    # TS-R2: the worker outlives the sibling by 1.5 s rather than 4 s.
    r, _ = _docker_project(tmp_path, monkeypatch, kill_delay=2.5, sleeper_delay=1)
    counter = [10]
    monkeypatch.setattr(DockerExecutor, "oom_kill_count", lambda self: counter[0], raising=False)

    async def scenario():
        killed = (await r.start("worker", "gets killed later"))["agent_id"]
        sibling = (await r.start("sleeper", "short-lived sibling"))["agent_id"]
        assert r.tree.get(sibling).status == "running"
        counter[0] = 11                            # OOM while both are in there
        await asyncio.wait_for(r.runs[sibling].done.wait(), 15)
        assert r.tree.get(killed).status == "running", "fixture: worker must outlive the sibling"
        await asyncio.wait_for(r.runs[killed].done.wait(), 15)
        return killed

    killed = asyncio.run(scenario())
    hits = _hits_for(r, killed)
    assert len(hits) == 1, hits
    assert hits[0]["effect"] == "kill_uncertain", hits[0]
    assert hits[0]["key"] == "process.sigkill", hits[0]


# ---------------------------------------------------------------------------
# A2 — the monitor reads the forgeable mirror; a malformed one must not break it


@pytest.mark.parametrize("forged", [
    "not-a-mapping",
    {"active": {"x|tree": 1}},
    {"active": {"a|tree": {"first_hit": "yesterday"}, "b|tree": {"first_hit": 2.0}}},
    {"active": {"x|tree": {"message": "m", "count": "many"}}},
], ids=["string", "entry-int", "mixed-first-hit", "count-string"])
def test_adv_forged_mirror_in_tree_json_does_not_crash_the_monitor(tmp_path, monkeypatch, forged):
    """`tree.json` is container-writable; its `limit_notices` block is display
    only. A container agent writing junk there must not take the monitor's
    snapshot down."""
    r, _, _ = _project(tmp_path, monkeypatch)
    _node(r, "ag-existing")
    with r.tree.transaction() as data:
        data[notices.STATE_KEY] = forged
    snap = snapshot(r.paths, r.config, with_scripts=False)
    assert "alerts" in snap


# ---------------------------------------------------------------------------
# A3 — a corrupt host-side state fails safe instead of crashing


_CORRUPT = {
    "active-entry-string": {"active": {"limits.max_concurrent|tree": "x"}},
    "active-count-string": {"active": {"limits.max_concurrent|tree": {"count": "abc"}}},
    "active-count-null": {"active": {"limits.max_concurrent|tree": {"count": None}}},
    "active-entry-no-key": {"active": {"a|tree": {"effect": "refused"}}},
    "cursor-string": {"cursors": {"ag-caller": "12"}},
    "first-hit-mixed": {"active": {"a|tree": {"first_hit": "z"}, "b|tree": {"first_hit": 1.0}}},
}


@pytest.mark.parametrize("name", sorted(_CORRUPT))
def test_adv_corrupt_host_notice_state_does_not_crash_hit_clear_or_since(tmp_path, monkeypatch, name):
    """`notices.py`'s own promise: "Anything unreadable resets the dedup".
    `_clean` only checks the top-level shape, so a well-formed JSON file with
    a malformed entry raises out of `hit` (a start_agent refusal), `clear`
    (the next successful start) or `since` (every wait_for_agents)."""
    r, _, _ = _project(tmp_path, monkeypatch)
    _node(r, "ag-caller", status="idle")
    state = notices._state(r.tree)
    state.file.write_text(json.dumps(_CORRUPT[name]))
    _hit(r.tree)
    notices.clear(r.tree, lambda e: e.get("effect") == "refused")
    notices.since(r.tree, "ag-caller")
    notices.since(r.tree, "root")


def test_adv_corrupt_host_state_does_not_turn_a_refusal_into_a_crash(tmp_path, monkeypatch):
    """End to end: with a malformed entry in the host file, a start refused by
    `max_concurrent` still fails with the refusal (RuntimeError/PermissionError),
    not an internal error."""
    r, _, _ = _project(tmp_path, monkeypatch, project_lines=["max_concurrent: 1"])
    _node(r, "ag-existing")
    notices._state(r.tree).file.write_text(
        json.dumps({"active": {"limits.max_concurrent|tree": "x"}}))
    try:
        result = _try_start(r)
    except Exception as exc:                        # noqa: BLE001
        pytest.fail(f"refusal became {type(exc).__name__}: {exc}")
    assert isinstance(result, RuntimeError), result


# ---------------------------------------------------------------------------
# A4 — LN-C4 cursors: a log that shrank (the case `since` itself names)


def test_adv_a_shrunk_event_log_does_not_replay_notices_on_every_wait(tmp_path, monkeypatch):
    """`since` handles "a log that shrank underneath us" by re-reading from the
    caller's creation, but then stores max(old cursor, new size): the cursor
    stays past the end, so every later wait takes the first-call path again
    and re-shows the same notices (and, once the log regrows past the stale
    cursor, skips everything written in between)."""
    r, _, _ = _project(tmp_path, monkeypatch)
    _node(r, "ag-caller", status="idle")
    # grow the log well past what it will be after the truncation
    for i in range(50):
        r.tree.emit("-", "filler", n=i, pad="x" * 100)
    assert notices.since(r.tree, "ag-caller") == []
    r.paths.events_file.write_text("")             # rotated / truncated by the operator
    _hit(r.tree)
    first = notices.since(r.tree, "ag-caller")
    assert [n["kind"] for n in first] == ["limit_hit"], first
    second = notices.since(r.tree, "ag-caller")
    assert second == [], f"the same notice was shown again: {second}"


# ---------------------------------------------------------------------------
# A5 — the display mirror is written outside the state lock


def test_adv_a_stale_mirror_cannot_resurrect_a_cleared_notice(tmp_path, monkeypatch):
    """Two servers: A records a hit and releases the state lock, B clears it
    and mirrors "nothing active", then A's delayed mirror lands. The host
    state says cleared; the monitor shows the limit as constraining work,
    until some unrelated notice happens to rewrite the mirror."""
    r, _, _ = _project(tmp_path, monkeypatch)
    _node(r, "ag-existing")
    real_mirror = notices._mirror
    hit_committed, clear_done = threading.Event(), threading.Event()
    # TS-R2: set when B has to wait for the state lock. A mirror written under
    # that lock (the fix) makes B wait for A, so A must stop holding its mirror
    # back once B is blocked; it used to wait out a 10 s timeout instead.
    clear_blocked = threading.Event()
    gate = {"hold": True}

    def slow_mirror(tree, state):
        if gate["hold"] and threading.current_thread().name == "server-a":
            gate["hold"] = False
            hit_committed.set()
            for _ in range(1000):
                if clear_done.is_set() or clear_blocked.is_set():
                    break
                time.sleep(0.01)
        real_mirror(tree, state)

    class Flock:
        def __getattr__(self, name):
            return getattr(fcntl, name)

        def flock(self, fd, op):
            if op == fcntl.LOCK_EX and threading.current_thread().name != "server-a":
                try:
                    return fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    clear_blocked.set()
            return fcntl.flock(fd, op)

    monkeypatch.setattr(notices, "_mirror", slow_mirror)
    monkeypatch.setattr(notices, "fcntl", Flock())
    a = threading.Thread(target=_hit, args=(r.tree,), name="server-a")
    a.start()
    assert hit_committed.wait(10)
    ended = notices.clear(r.tree, lambda e: e.get("effect") == "refused")
    assert len(ended) == 1
    clear_done.set()
    a.join(10)

    assert notices.NoticeState(r.tree).read()["active"] == {}, "fixture: host state is cleared"
    alerts = [a for a in snapshot(r.paths, r.config, with_scripts=False)["alerts"]
              if a.get("kind") == "limit"]
    assert not alerts, f"a cleared notice is shown as active: {alerts}"


# ---------------------------------------------------------------------------
# A6 — LN-C2 finding 8 for ADOPTED runs: provenance is read back from tree.json


def test_adv_adopted_run_provenance_is_not_taken_from_container_writable_tree_json(tmp_path, monkeypatch):
    """Finding 8 says a trip names the value and source captured at launch.
    For a run adopted after a restart `_trip_notice` reads them from the node
    record in `tree.json` — which the running agent can write. Forge that
    record between the two servers and the `stuck` notice names the forged
    file, line and value instead of agents.yaml's."""
    r_old, _, agent_file = _project(tmp_path, monkeypatch, agent_lines=["timeout: 2"], delay=4,
                                    retry=False)
    real_file = str(agent_file.resolve())

    async def scenario():
        node_id = (await r_old.start("worker", "outlives its server"))["agent_id"]
        await r_old.shutdown(detach=True)
        forged = {"value": 2, "source": "agent",
                  "source_detail": {"layer": "agent", "file": "/forged/elsewhere.yaml",
                                    "line": 999}}
        with r_old.tree.transaction() as data:
            node = data["nodes"][node_id]
            for field in ("limits", "effective_limits"):
                if isinstance(node.get(field), dict):
                    node[field]["timeout"] = dict(forged)
        r_new = Runner(r_old.paths, load(r_old.paths, seed=False))
        adopted = await r_new.adopt()
        assert node_id in adopted, adopted
        await asyncio.wait_for(r_new.runs[node_id].done.wait(), 30)
        return node_id, r_new

    node_id, r_new = asyncio.run(scenario())
    hits = [e for e in _events(r_new, "limit_hit") if e.get("node") == node_id]
    assert hits, _events(r_new, "limit_hit")
    source = hits[0]["source"] or {}
    assert source.get("file") == real_file, source
    assert "/forged/" not in hits[0]["message"], hits[0]["message"]


# ---------------------------------------------------------------------------
# A7 — a malformed occupancy record must not orphan a launched run


def test_adv_malformed_occupancy_entry_does_not_orphan_a_started_run(tmp_path, monkeypatch):
    """`ContainerOccupancy._reconcile` catches TypeError/ValueError only; an
    entry that is not a mapping raises AttributeError from `register`, which
    runs AFTER the docker process started and BEFORE its consumer task exists.
    The start fails with an internal error while the agent keeps running,
    unwatched, never finalised."""
    from multiagents import runner as runner_mod
    from multiagents.occupancy import ContainerOccupancy

    r, _ = _docker_project(tmp_path, monkeypatch, kill_delay=1)
    monkeypatch.setattr(DockerExecutor, "oom_kill_count", lambda self: 10, raising=False)
    container = str(runner_mod.get_executor().container or "")
    occ = ContainerOccupancy(r.paths)
    occ.file.write_text(json.dumps({container: {"ag-ghost": None}}))

    async def scenario():
        try:
            node_id = (await r.start("worker", "work"))["agent_id"]
        except Exception as exc:                     # noqa: BLE001
            live = [n for n in r.tree.read()["nodes"].values()
                    if n.get("agent") == "worker"]
            return exc, live
        await asyncio.wait_for(r.runs[node_id].done.wait(), 15)
        return None, [r.tree.read()["nodes"][node_id]]

    exc, nodes = asyncio.run(scenario())
    assert exc is None, f"start raised {type(exc).__name__}: {exc}; nodes: " \
        f"{[(n['id'], n['status'], n.get('pid')) for n in nodes]}"
    assert nodes[0]["status"] not in ("running", "pending"), nodes[0]["status"]


@pytest.mark.parametrize("entry", [None, 1, "x", []], ids=["null", "int", "str", "list"])
def test_adv_occupancy_with_a_malformed_entry_fails_closed_without_raising(tmp_path, monkeypatch, entry):
    from multiagents.occupancy import ContainerOccupancy
    from multiagents.paths import ProjectPaths

    (tmp_path / "p").mkdir()
    occ = ContainerOccupancy(ProjectPaths(tmp_path / "p"))
    occ.file.write_text(json.dumps({"c": {"ag-ghost": entry}}))
    assert occ.others("c", "ag-me") is True          # unknown -> conservative
    occ.register("c", "ag-me", 0)
    occ.forget("c", "ag-me")


# ---------------------------------------------------------------------------
# Coverage guards. These PASS on 942df35; each kills a mutation the existing
# D1 suite let survive (see the adversary report).


def _hit_worker(args):
    state_dir, tree_file, events_file, n = args
    import os
    os.environ["MULTIAGENTS_STATE_DIR"] = state_dir
    from multiagents import notices as mod
    from multiagents.tree import Tree
    tree = Tree(Path(tree_file), Path(events_file))
    for _ in range(n):
        mod.hit(tree, key="limits.max_concurrent", value=1, effect="refused",
                scope="tree", source=None, message="m")


def test_guard_concurrent_hits_from_many_processes_announce_once_and_count_all(tmp_path, monkeypatch):
    """LN-C4 across processes, at the same instant: one `limit_hit`, and a
    count equal to every hit (kills: dropping the flock in `locked`)."""
    import multiprocessing
    r, _, _ = _project(tmp_path, monkeypatch)
    import os
    args = (os.environ["MULTIAGENTS_STATE_DIR"], str(r.paths.tree_file),
            str(r.paths.events_file), 25)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(8) as pool:
        pool.map(_hit_worker, [args] * 8)
    hits = [e for e in _events(r, "limit_hit") if e.get("key") == "limits.max_concurrent"]
    assert len(hits) == 1, len(hits)
    state = notices.NoticeState(r.tree).read()
    assert state["active"]["limits.max_concurrent|tree"]["count"] == 200


def test_guard_no_container_visible_file_can_suppress_a_real_hit(tmp_path, monkeypatch):
    """Finding 3 generalised: F3 forges only `tree.json`. Copy EVERY file a
    genuine hit changes under the project root (all container-visible) into a
    fresh project; the next real refusal there must still announce itself
    (kills: keeping the notice state under the project directory)."""
    donor, _, _ = _project(tmp_path / "donor", monkeypatch, project_lines=["max_concurrent: 1"])
    _node(donor, "ag-existing")

    def files(root):
        return {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.parts}

    before = files(donor.paths.root)
    assert isinstance(_try_start(donor), RuntimeError)
    after = files(donor.paths.root)
    changed = {rel: data for rel, data in after.items()
               if before.get(rel) != data and rel.name != "events.jsonl"}
    assert changed

    victim, _, _ = _project(tmp_path / "victim", monkeypatch, project_lines=["max_concurrent: 1"])
    _node(victim, "ag-existing")
    for rel, data in changed.items():
        target = victim.paths.root / rel
        if rel.name == "tree.json":
            merged = json.loads(target.read_text())
            merged.update({k: v for k, v in json.loads(data).items() if k != "nodes"})
            target.write_text(json.dumps(merged))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    assert isinstance(_try_start(victim), RuntimeError)
    assert len([e for e in _events(victim, "limit_hit")
                if e.get("key") == "limits.max_concurrent"]) == 1


def _occupancy(tmp_path):
    from multiagents.occupancy import ContainerOccupancy
    from multiagents.paths import ProjectPaths
    (tmp_path / "p").mkdir(exist_ok=True)
    return ContainerOccupancy(ProjectPaths(tmp_path / "p"))


def test_guard_forget_frees_the_slot(tmp_path):
    """kills: `forget` as a no-op (a finished run stays an occupant for as long
    as its server lives, and every later SIGKILL is `kill_uncertain`)."""
    import os
    occ = _occupancy(tmp_path)
    occ.register("c", "ag-a", os.getpid())
    assert occ.others("c", "ag-b") is True
    occ.forget("c", "ag-a")
    assert occ.others("c", "ag-b") is False


def test_guard_a_live_run_whose_server_died_is_still_an_occupant(tmp_path):
    """kills: `or` -> `and` in `_reconcile` (a sibling left running by a
    crashed server would vanish and a later OOM be misattributed)."""
    import subprocess
    from multiagents import procs
    occ = _occupancy(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        occ.file.write_text(json.dumps({"c": {"ag-sibling": {
            "pid": child.pid, "pid_start": procs.start_time(child.pid),
            "owner_pid": dead.pid, "owner_start": "0", "since": 0}}}))
        assert occ.others("c", "ag-me") is True
    finally:
        child.kill()
        child.wait()


def test_guard_occupancy_without_a_usable_record_says_crowded(tmp_path, monkeypatch):
    """kills: the degraded (memory) branch answering "alone" — LN-C5's
    attribution must fail closed."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(blocker))
    occ = _occupancy(tmp_path)
    assert occ.memory is not None, "fixture: the record directory must be unusable"
    assert occ.others("c", "ag-me") is True
