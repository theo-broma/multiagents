"""TB2-R6 — a dirty session-alias worktree is cleaned, not left to hold.

Black box on the in-process scheduler fixtures (`nc_fixture.m4_world`) with a
fake provider. The reviewer `rv` of the `PAIR` loop carries `session: B`; its
first activation dirties the alias checkout, the second activation is the
"waiting node". The reviewer's queue is consumed in launch order, so the entry
after the dirtying one is the CLEANUP run (when the scheduler resumes the
session) and the one after that is the real second activation.

Assumptions where the spec is silent (kept loose):
- the `cleanup_requested` event is a `node.cleanup_requested` line of the event
  log that names the waiting node, mentions an alias and a run.
- the archive is somewhere under the scheduler's host state directory, and the
  `hold.detail` of `dirty_worktree` contains an absolute path to it (file or
  directory) from which the archived content is reachable.
- "bounded" is tested with a very large tracked diff and a prompt cap of
  `PROMPT_CAP` characters (the spec gives no number).
- NOT TESTED: staged-only changes (the fixture agent cannot `git add` without
  committing); session gone / quota / timeout / cancel causes of a failed
  cleanup (only a crashing cleanup run can be forced); the admission-versus-
  preflight distinction (one end behaviour covers both).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402

PAIR = """\
template: tb2r6
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

WAIT = 8
WAIT_ROUNDS = 20
PROMPT_CAP = 100_000
UNTRACKED = "UNTRACKED-BODY-MARKER"
TRACKED = "TRACKED-EDIT-MARKER"
GITIGNORE = ".venv/\n__pycache__/\n"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def start(w, rounds=3):
    w.start_scheduler()
    w.register_ok(PAIR)
    top = w.instantiate_ok("tb2r6", {"rounds": rounds})
    wk, rv = w.children(top)
    return top, wk, rv


def finish(w, top, rv, timeout=WAIT_ROUNDS):
    """Wait for the loop to be done; a hold on the waiting node fails at once."""
    def probe():
        n = w.get(top)
        if n["state"] == "done":
            return n
        held = w.get(rv)
        if held["state"] == "held":
            raise AssertionError(f"the waiting node was held instead of launching: {held['hold']}")
        return None
    return w.until(probe, timeout, what="the loop to finish")


def cleanup_started(w, rv, count=2, timeout=WAIT_ROUNDS):
    def probe():
        if len(w.fxr.calls()) >= count:
            return w.fxr.calls()[count - 1]
        held = w.get(rv)
        if held["state"] == "held":
            raise AssertionError(f"held without a cleanup run: {held['hold']}")
        return None
    return w.until(probe, timeout, what="the cleanup run to start")


def worker_two_rounds(w):
    w.fxw.queue(commit_entry(".gitignore", GITIGNORE, "r1"), commit_entry("b.txt", "two\n", "r2"))


def dirtying(tracked=None, **more):
    """First reviewer activation: leaves diff.patch untracked and, with
    `tracked`, an edit of the tracked .gitignore (text appended)."""
    leave = {"diff.patch": UNTRACKED + "\n"}
    if tracked:
        leave[".gitignore"] = GITIGNORE + tracked
    return verdict_entry("rejected", [finding("redo")], leave=leave, **more)


def cleaner(**more):
    return {"delete": ["diff.patch"], **more}


def approve():
    return verdict_entry("approved")


def cleanup_events(w, rv):
    return [e for e in w.events() if w.kind_of(e) == "node.cleanup_requested" and rv in json.dumps(e)]


def assert_event_names_alias_and_run(w, rv, cleanup_call):
    """The event names the waiting node, the alias and the cleanup run id."""
    ev = cleanup_events(w, rv)
    assert ev, "no node.cleanup_requested"
    text = json.dumps(ev[0])
    assert '"B"' in text, f"the event does not name the alias B: {ev[0]}"
    run_id = (cleanup_call.get("mcp_env") or {}).get("MULTIAGENTS_AGENT_ID")
    assert run_id, f"the fixture exposes no run id for the cleanup run: {cleanup_call.get('mcp_env')}"
    assert run_id in text, f"the event does not name the cleanup run {run_id}: {ev[0]}"


def host_texts(w):
    out = []
    for p in w.scheduler_files():
        try:
            out.append((p, p.read_bytes().decode("utf-8", "replace")))
        except OSError:
            pass
    return out


def archive_holds(w, marker):
    return any(marker in text for _, text in host_texts(w))


def detail_path(hold):
    m = re.findall(r"(/[^\s\"',;)]+)", json.dumps(hold))
    return [Path(x) for x in m if Path(x).exists()]


def reachable_text(path):
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in files)


def cleanup_calls(w):
    """Reviewer spawns that started on a checkout still holding the leftover."""
    return [c for c in w.fxr.calls() if "diff.patch" in c["files"]]


# ------------------------------------------------- ignored files never hold

def test_tb2_r6_ignored_only_worktree_launches_without_hold_or_cleanup(w):
    worker_two_rounds(w)
    w.fxr.queue(verdict_entry("rejected", [finding("redo")],
                              write={".venv/pyvenv.cfg": "x\n", "__pycache__/m.pyc": "y\n"}),
                approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    assert w.fxr.spawns() == 2, "a cleanup (or a hold) happened for ignored files"
    assert cleanup_events(w, rv) == []
    assert "held" not in w.transitions(rv)
    second = w.fxr.calls()[1]
    assert ".venv" not in second["files"] and "__pycache__" not in second["files"], "ignored files were not removed"


def test_tb2_r6_ignored_files_are_neither_listed_nor_present_when_a_real_leftover_triggers_cleanup(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(write={".venv/pyvenv.cfg": "x\n"}), cleaner(), approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    cleanup = cleanup_calls(w)[0]
    assert ".venv" not in cleanup["files"], "ignored files must be removed before the check"
    assert ".venv" not in cleanup["prompt"]


# ------------------------------------------- the successful cleanup path

def test_tb2_r6_untracked_file_costs_one_cleanup_on_the_previous_session_then_the_node_launches(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(gate="c1"), approve())
    top, wk, rv = start(w)
    first = w.until(lambda: (w.fxr.calls() or [None])[0], WAIT, what="first reviewer run")
    cleanup = cleanup_started(w, rv)
    # while the cleanup runs: visible, nothing else launched on the alias
    assert cleanup_events(w, rv), "no node.cleanup_requested while the cleanup runs"
    assert_event_names_alias_and_run(w, rv, cleanup)
    assert w.fxr.spawns() == 2
    assert cleanup["resume"] == first["session"], "the cleanup is not the alias's previous conversation"
    assert cleanup["model"] == first["model"] and cleanup["cwd"] == first["cwd"]
    assert "diff.patch" in cleanup["files"]
    w.gate("c1", w.fxr)
    done = finish(w, top, rv)
    assert done["outcome"] == "approved"
    third = w.fxr.calls()[2]
    assert third["resume"] == first["session"] and "diff.patch" not in third["files"]
    assert len(cleanup_events(w, rv)) == 1


def test_tb2_r6_cleanup_runs_at_the_alias_previous_commit_not_the_waiting_nodes_input(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(), approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    _, cleanup, third = w.fxr.calls()
    assert "b.txt" not in cleanup["files"], "the cleanup ran at the waiting node's input, not the alias's previous commit"
    assert cleanup["head"] != third["head"]
    assert "b.txt" in third["files"]


def test_tb2_r6_cleanup_message_names_the_dirty_files_and_carries_no_untracked_contents(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(), approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    prompt = w.fxr.calls()[1]["prompt"]
    assert "diff.patch" in prompt
    assert UNTRACKED not in prompt, "an untracked file's contents leaked into the message"
    low = prompt.lower()
    assert "commit" in low and "clean" in low, "the message does not ask for a clean, uncommitted worktree"
    assert re.search(r"remove|revert|delete", low) and "each" in low, "the message does not ask to remove or revert each listed item"
    assert re.search(r"reply|tell|say|report|explain", low) and re.search(r"kept|keep|should\s+(have\s+been|stay|remain)", low), \
        "the message does not ask the agent to say in its reply what it believes should have been kept"


# NOTE: the tracked-edit tests below currently fail at the check itself with
# `reseat_failed ... external diff died` (src/multiagents/gitops.py runs
# `git -c diff.external= diff HEAD`, which dies on any tracked change). That is
# a defect outside TB2-R6; they are kept to the two behaviours that need a
# tracked change, and turn green once the diff can be read.

def test_tb2_r6_cleanup_message_carries_the_tracked_diff_inline_but_no_untracked_contents(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(tracked="# " + TRACKED + "\n"),
                cleaner(write={".gitignore": GITIGNORE}), approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    prompt = w.fxr.calls()[1]["prompt"]
    assert ".gitignore" in prompt and TRACKED in prompt, "the tracked change is not inline"
    assert UNTRACKED not in prompt


def test_tb2_r6_archive_holds_the_tracked_diff_before_the_cleanup_runs(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(tracked="# " + TRACKED + "\n"),
                cleaner(gate="c1", write={".gitignore": GITIGNORE}), approve())
    top, wk, rv = start(w)
    cleanup_started(w, rv)
    assert archive_holds(w, TRACKED), "the tracked diff is not archived before the cleanup"
    w.gate("c1", w.fxr)
    finish(w, top, rv)


def test_tb2_r6_cleanup_message_is_bounded_for_a_huge_dirty_state(w):
    worker_two_rounds(w)
    huge = "".join("line %d of a very large untracked file\n" % i for i in range(40_000))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")],
                              leave={"diff.patch": UNTRACKED * 50_000, "notes.txt": huge}),
                {"delete": ["diff.patch", "notes.txt"]}, approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    prompt = w.fxr.calls()[1]["prompt"]
    assert len(prompt) < PROMPT_CAP, len(prompt)
    assert UNTRACKED not in prompt


def test_tb2_r6_archive_is_on_the_host_with_status_and_untracked_contents_before_the_cleanup_runs(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(gate="c1"), approve())
    top, wk, rv = start(w)
    cleanup_started(w, rv)
    # the cleanup agent is still running (gated): the archive must already exist
    assert archive_holds(w, UNTRACKED), "untracked file contents are not archived before the cleanup"
    assert any("diff.patch" in text for _, text in host_texts(w))
    w.gate("c1", w.fxr)
    finish(w, top, rv)
    assert archive_holds(w, UNTRACKED), "the archive did not survive the cleanup"


def test_tb2_r6_cleanup_produces_no_generation_no_verdict_no_round_and_no_commit(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(), approve())
    top, wk, rv = start(w)
    finish(w, top, rv)
    assert len(w.fxr.verdicts()) == 2, "the cleanup run gave or recorded a verdict"
    assert w.fxr.calls()[1]["verdict"] is None
    assert len(w.get(top)["generations"]) == 2, "the cleanup run recorded a generation"
    assert w.get(top)["loop"]["rounds_rejected"] == 1, "the cleanup run counted a loop round"
    assert w.git("log", "--all", "--format=%H", "--", "diff.patch").stdout.strip() == "", "WIP commit of the leftover"


# --------------------------------------------- still dirty / failed cleanup

def test_tb2_r6_a_cleanup_that_leaves_the_file_holds_dirty_worktree_with_the_archive_and_no_second_cleanup(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), {}, approve())
    top, wk, rv = start(w)
    held = w.wait_held(rv, "dirty_worktree", timeout=WAIT_ROUNDS)
    w.quiet(2)
    assert w.fxr.spawns() == 2, "a second cleanup or a launch followed the failed cleanup"
    assert w.get(rv)["state"] == "held"
    assert len(cleanup_events(w, rv)) == 1
    paths = detail_path(held["hold"])
    assert paths, f"the hold detail carries no existing archive path: {held['hold']}"
    assert any(UNTRACKED in reachable_text(p) for p in paths), "the archive path does not lead to the untracked file"
    leftover = Path(w.fxr.calls()[0]["cwd"]) / "diff.patch"
    assert leftover.read_text().startswith(UNTRACKED), "the leftover was deleted by the scheduler"
    assert all(c["resume"] for c in w.fxr.calls()[1:]), "a fresh conversation was started"


def test_tb2_r6_a_cleanup_run_that_fails_holds_dirty_worktree_and_starts_no_fresh_conversation(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), {"crash": True}, approve())
    top, wk, rv = start(w)
    held = w.wait_held(rv, "dirty_worktree", timeout=WAIT_ROUNDS)
    w.quiet(2)
    assert w.fxr.spawns() == 2
    assert [c["resume"] for c in w.fxr.calls()][1] == w.fxr.calls()[0]["session"]
    assert detail_path(held["hold"]), held["hold"]
    assert len(w.fxr.verdicts()) == 1 and w.get(top)["loop"]["rounds_rejected"] == 1


def test_tb2_r6_a_scheduler_restart_after_the_cleanup_does_not_start_another(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), {}, approve())
    top, wk, rv = start(w)

    def cleanup_settled():
        node = w.get(rv)
        if node["state"] == "held" and node["hold"]["reason"] == "dirty_worktree":
            return node
        # A loop child is briefly done after a rejected round, before its
        # next activation. Only a terminal loop makes the hold impossible.
        loop = w.get(top)
        if loop["state"] in ("done", "cancelled"):
            raise AssertionError(f"the loop finished before cleanup held the reviewer: {loop}")
        return None

    w.until(cleanup_settled, WAIT_ROUNDS, what="the failed cleanup to hold the reviewer")
    w.restart_scheduler()
    # A listening socket alone does not prove the restarted scheduler has
    # evaluated the persisted cleanup. The next tick proves its first
    # reconciliation/admission pass finished, however slow the host is.
    first_tick = w.until(lambda: w.status().get("last_tick"), WAIT,
                         what="the restarted scheduler's first tick")
    w.until(lambda: w.status().get("last_tick") != first_tick, WAIT,
            what="the restarted scheduler to finish its first tick")
    assert w.fxr.spawns() == 2, "the restart granted a second cleanup"
    assert len(cleanup_calls(w)) == 1
    assert w.get(rv)["state"] == "held"


def test_tb2_r6_a_scheduler_killed_during_the_cleanup_does_not_start_a_second_one(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), cleaner(gate="c1"), cleaner(), approve())
    top, wk, rv = start(w)
    cleanup_started(w, rv)
    w.kill9()
    w.fxr.open_gate("c1")
    w.start_scheduler()

    def settled():
        n = w.get(rv)
        return n["state"] in ("held", "done") or w.get(top)["state"] in ("done", "held") or None
    w.until(settled, WAIT_ROUNDS, what="the node to settle after the restart")
    w.quiet(2)
    assert len(cleanup_calls(w)) <= 1, "a second cleanup of the same activation was started"
    assert len(cleanup_events(w, rv)) <= 1


# ------------------------------------------------------ new activation

def test_tb2_r6_relaunch_node_allows_a_new_cleanup(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(), {}, cleaner(), approve())
    top, wk, rv = start(w)
    w.wait_held(rv, "dirty_worktree", timeout=WAIT_ROUNDS)
    assert w.fxr.spawns() == 2
    reply = w.root_op("relaunch_node", rv)
    assert reply.get("ok") is True, reply
    w.wait_state(top, "done", timeout=WAIT_ROUNDS)
    assert len(cleanup_events(w, rv)) == 2, "the new activation did not get its own cleanup"
    assert w.fxr.spawns() == 4
