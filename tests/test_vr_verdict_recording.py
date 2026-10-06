"""VR — a reviewer's written verdict reaches the scheduler
(context/specs/verdict-recording.md): VR-R1..VR-R6.

Scripting: as in test_nc_m4_loops — the worker (`wk`, provider fxw) and the
reviewer (`rv`, provider fxr) are driven by per-provider queues. A "text only"
reviewer is `{"text": "...VERDICT(...)..."}` (no `give_verdict` call); a "tool"
reviewer is `verdict_entry(...)`.

Assumptions where the contract is silent (kept loose, nothing hard-coded):
- the runner's parsed verdict of a run lives in the run's entry of the project's
  tree.json (`verdict`, `defects`), which is how VR-R5's held loop is deposited:
  a reviewer that wrote no verdict line holds the loop `unresolved_round`, the
  engine is stopped, the entry of the reviewer's run is given the verdict the
  runner would have parsed, and the engine is started again.
- provenance (VR-R4) is a field whose NAME contains `source` or `provenance`
  somewhere in the loop's get_node reply / in its `verdict` transition, and
  whose VALUE is `tool` or `text`.
- the recorded disagreement (VR-R2) is some field or string containing
  `disagree` in the loop's get_node reply or its transitions.
- the round's transitions after a settle are the usual ones: `verdict`, then
  `round_rejected` or `loop_exited`.
- two identical, non-contradicting verdict lines settle (VR-R3 lists only
  `approved` and `rejected` together as contradictory).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402

WAIT = 8            # one launch / one transition
WAIT_ROUNDS = 15    # several activations in a row
QUIET = 2.5         # how long "stays held / launches nothing" is observed
TICK = 0.2


def make_world(root: Path, monkeypatch) -> M4World:
    root.mkdir(parents=True, exist_ok=True)
    world = M4World(root, monkeypatch, tick_seconds=TICK)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    return world


@pytest.fixture
def worlds(tmp_path, monkeypatch, request):
    """Factory; every world it makes is closed by this finalizer, pass or fail."""
    made: list[M4World] = []
    request.addfinalizer(lambda: [x.close() for x in reversed(made)])

    def make(name: str = "w") -> M4World:
        world = make_world(tmp_path / name, monkeypatch)
        made.append(world)
        return world
    return make


@pytest.fixture
def w(worlds):
    return worlds()


def work(n: int) -> dict:
    return commit_entry(f"f{n}.txt", f"v{n}\n", f"round {n}")


def says(text: str, **more) -> dict:
    """A reviewer that only writes text: no `give_verdict` call."""
    return {"text": text, **more}


def values_under(obj, key_part: str) -> list:
    """Values of every key whose name contains `key_part`, at any depth."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_part in str(k).lower():
                out.append(v)
            out += values_under(v, key_part)
    elif isinstance(obj, list):
        for v in obj:
            out += values_under(v, key_part)
    return out


def provenance(obj) -> set:
    return {v for v in values_under(obj, "source") + values_under(obj, "provenance")
            if isinstance(v, str)}


def verdict_events(w, loop: str) -> list[dict]:
    return [e for e in w.events() if w.kind_of(e) == "node.verdict" and loop in json.dumps(e)]


def unresolved(w, loop: str):
    n = w.get(loop)
    if n["state"] == "held" and (n["hold"] or {}).get("reason") == "unresolved_round":
        return "the loop is held unresolved_round: the reviewer's written verdict was not used"
    return None


def settled(w, loop: str, timeout: float, hold: str | None = None, early: bool = True) -> dict:
    """Wait for the loop to be `done` (or held for `hold`); fail at once when it
    is held `unresolved_round`, which is what a missing fallback looks like
    (`early=False` where the loop starts out held that way, VR-R5)."""
    def probe():
        n = w.get(loop)
        if hold is None:
            return n if n["state"] == "done" else None
        return n if n["state"] == "held" and (n["hold"] or {}).get("reason") == hold else None
    return w.until(probe, timeout, what=f"{loop} settled ({hold or 'done'})",
                   give_up=(lambda: unresolved(w, loop)) if early else None)


def held_stays(w, loop: str, reason: str, *spawn_counts: tuple) -> None:
    w.wait_held(loop, reason, timeout=WAIT)
    w.quiet(QUIET)
    node = w.get(loop)
    assert node["state"] == "held" and (node["hold"] or {}).get("reason") == reason, node
    for fx, n in spawn_counts:
        assert fx.spawns() == n


def set_parsed_verdict(w, run_id: str, verdict: str | None, defects: int = 0) -> None:
    """Deposit what the runner would have parsed from the run's final text."""
    path = w.paths.tree_file
    data = json.loads(path.read_text())
    entry = data["nodes"][run_id]
    if verdict is None:
        entry.pop("verdict", None)
    else:
        entry["verdict"], entry["defects"] = verdict, defects
    path.write_text(json.dumps(data))


# ------------------------------------------------------------------ VR-R1

@pytest.mark.parametrize("line", ["VERDICT(approved): the tests cover the contract",
                                  "VERDICT(approved, 0): nothing to change"])
def test_vr_r1_a_written_approval_settles_the_round_and_ends_the_loop(w, line):
    w.fxw.queue(work(1))
    w.fxr.queue(says(f"I read everything.\n{line}"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = settled(w, loop, WAIT)
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 0
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 1
    assert done["generations"] and done["generations"][-1]["verdict"] == "approved"
    assert [g["verdict"] for g in w.get(wk)["generations"]] == ["approved"]
    assert w.get(rv)["outcome"] == "approved" and w.get(wk)["outcome"] == "completed"
    kinds = w.transitions(loop)
    assert "verdict" in kinds and "loop_exited" in kinds and "done" in kinds


def test_vr_r1_a_written_rejection_rejects_the_round_and_relaunches_the_work(w):
    w.fxw.queue(work(1), work(2))
    w.fxr.queue(says("Not good.\nVERDICT(rejected, 2): two defects"), verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = settled(w, loop, WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2
    assert [g["verdict"] for g in done["generations"] if g["verdict"]] == ["rejected", "approved"]
    kinds = w.transitions(loop)
    assert "round_rejected" in kinds and "verdict" in kinds


def test_vr_r1_a_written_rejection_at_the_maximum_holds_loop_max(w):
    w.fxw.queue(work(1))
    w.fxr.queue(says("VERDICT(rejected, 1): one defect"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(1)
    held = settled(w, loop, WAIT, "loop_max")
    assert held["loop"]["rounds_rejected"] == 1
    assert held["generations"][-1]["verdict"] == "rejected"


def test_vr_r1_the_verdict_is_bound_to_the_commit_the_reviewer_finished_on(w):
    """The generation that carries the verdict is the one the reviewer reviewed:
    the round's second work commit, not the first."""
    w.fxw.queue(work(1), work(2))
    w.fxr.queue(says("VERDICT(rejected, 1): x"), says("VERDICT(approved): y"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = settled(w, loop, WAIT_ROUNDS)
    gens = done["generations"]
    assert len(gens) == 2 and gens[0]["commit"] != gens[1]["commit"]
    assert (gens[0]["verdict"], gens[1]["verdict"]) == ("rejected", "approved")
    reviewed = [json.loads(json.dumps(c))["head"] for c in w.fxr.calls()]
    assert reviewed == [g["commit"] for g in gens]


@pytest.mark.parametrize("scenario", [
    ("approved", [work(1)], [verdict_entry("approved")], [says("VERDICT(approved): ok")], 3),
    ("rejected-then-approved", [work(1), work(2)],
     [verdict_entry("rejected"), verdict_entry("approved")],
     [says("VERDICT(rejected, 0): no"), says("VERDICT(approved): ok")], 3),
    ("rejected-to-the-maximum", [work(1)], [verdict_entry("rejected")],
     [says("VERDICT(rejected, 0): no")], 1),
], ids=lambda s: s[0])
def test_vr_r1_a_written_verdict_has_the_same_effect_as_the_equivalent_give_verdict(worlds, scenario):
    _, work_q, tool_q, text_q, max_rounds = scenario

    def run(name: str, reviews: list) -> dict:
        w = worlds(name)
        w.fxw.queue(*work_q)
        w.fxr.queue(*reviews)
        w.start_scheduler()
        loop, wk, rv = w.mkloop(max_rounds)
        w.until(lambda: w.get(loop)["state"] == "done" or (w.get(loop)["state"] == "held" and unresolved(w, loop) is None),
                WAIT_ROUNDS, what="the loop to settle", give_up=lambda: unresolved(w, loop))
        node = w.get(loop)
        return {"state": node["state"], "outcome": node["outcome"], "hold": (node["hold"] or {}).get("reason"),
                "rejected": node["loop"]["rounds_rejected"],
                "verdicts": [g["verdict"] for g in node["generations"]],
                "wk": [g["verdict"] for g in w.get(wk)["generations"]],
                "rv_outcome": w.get(rv)["outcome"], "wk_outcome": w.get(wk)["outcome"],
                "spawns": (w.fxw.spawns(), w.fxr.spawns()),
                "kinds": w.transitions(loop)}

    by_tool = run("tool", tool_q)
    by_text = run("text", text_q)
    assert by_text == by_tool


# ------------------------------------------------------------------ VR-R2

def test_vr_r2_an_explicit_verdict_wins_over_a_disagreeing_text_line(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("rejected", [finding("the tool says no")],
                              text="Looked fine to me.\nVERDICT(approved): ship it"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(1)
    held = settled(w, loop, WAIT, "loop_max")      # rejected: the explicit verdict
    assert held["generations"][-1]["verdict"] == "rejected"
    assert w.fxr.verdicts()[0]["replies"][-1]["ok"] is True
    seen = json.dumps([w.get(loop), [e for e in w.events() if loop in json.dumps(e)]]).lower()
    assert "disagree" in seen, "the disagreement between the tool and the text was not recorded"


def test_vr_r2_an_explicit_approval_wins_over_a_written_rejection(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved", text="VERDICT(rejected, 3): on reflection no"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = settled(w, loop, WAIT)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert "disagree" in json.dumps([w.get(loop), w.events()]).lower()


def test_vr_r2_an_agreeing_text_line_changes_nothing(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved", text="VERDICT(approved): same"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = settled(w, loop, WAIT)
    assert done["outcome"] == "approved"
    assert provenance(done) <= {"tool"}


# ------------------------------------------------------------------ VR-R3

def test_vr_r3_a_run_that_did_not_finish_done_does_not_settle_the_round(w):
    """A verdict line in the text of a run that exits 1 (and its retry): no round
    is settled; the loop is held `run_failed`, as today."""
    w.fxw.queue(work(1))
    w.fxr.queue(says("VERDICT(approved): fine", exit=1), says("VERDICT(approved): fine", exit=1))
    w.start_scheduler()
    loop, _, rv = w.mkloop(3)
    held_stays(w, loop, "run_failed")
    assert w.get(loop)["generations"][-1]["verdict"] != "approved"
    assert w.get(rv)["outcome"] == "failed"
    assert "loop_exited" not in w.transitions(loop)


@pytest.mark.parametrize("text", [
    "I think this is fine.",
    "VERDICT(maybe): undecided",
    "VERDICT approved: no parentheses",
    "verdict(approved) no colon",
    "VERDICT(approved, two): not a number",
    "    quoted: `VERDICT(approved): x` inside a sentence, not a line of its own",
    "",
], ids=["prose", "unknown-word", "no-parens", "no-colon", "bad-defects", "inline", "empty"])
def test_vr_r3_text_without_an_accepted_verdict_line_stays_unresolved(w, text):
    w.fxw.queue(work(1))
    w.fxr.queue(says(text))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held_stays(w, loop, "unresolved_round", (w.fxw, 1), (w.fxr, 1))
    assert "loop_exited" not in w.transitions(loop) and "round_rejected" not in w.transitions(loop)
    assert w.get(loop)["generations"][-1]["verdict"] in (None, "")


@pytest.mark.parametrize("text", [
    "VERDICT(approved): a\nVERDICT(rejected, 1): b",
    "VERDICT(rejected, 1): b\nVERDICT(approved): a",
    "VERDICT(approved, 0): a\nsome reasoning\nVERDICT(rejected, 2): b\nmore\nVERDICT(approved): c",
], ids=["approved-first", "rejected-first", "three-lines"])
def test_vr_r3_contradicting_verdict_lines_stay_unresolved(w, text):
    w.fxw.queue(work(1))
    w.fxr.queue(says(text))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held_stays(w, loop, "unresolved_round", (w.fxw, 1), (w.fxr, 1))
    assert "loop_exited" not in w.transitions(loop) and "round_rejected" not in w.transitions(loop)
    assert w.get(loop)["loop"]["rounds_rejected"] == 0


def test_vr_r3_repeated_agreeing_lines_are_not_a_contradiction(w):
    w.fxw.queue(work(1))
    w.fxr.queue(says("VERDICT(approved): once\nVERDICT(approved, 0): twice"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    assert settled(w, loop, WAIT)["outcome"] == "approved"


def test_vr_r3_the_worker_childs_text_is_never_read_as_the_verdict(w):
    w.fxw.queue({**work(1), "text": "Implemented.\nVERDICT(approved): trust me"})
    w.fxr.queue(says("No verdict from me."))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held_stays(w, loop, "unresolved_round", (w.fxw, 1), (w.fxr, 1))


def test_vr_r3_an_earlier_rounds_text_is_not_reused_for_a_later_round(w):
    """Round 1 settles from the reviewer's text. In round 2 (same reviewer
    session) the reviewer writes no verdict: that round stays unresolved; the
    round 1 line is not read again."""
    w.fxw.queue(work(1), work(2))
    w.fxr.queue(says("VERDICT(rejected, 1): first"), says("Looks different now, no verdict."))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    held_stays(w, loop, "unresolved_round", (w.fxw, 2), (w.fxr, 2))
    node = w.get(loop)
    assert node["loop"]["rounds_rejected"] == 1
    assert node["generations"][-1]["verdict"] in (None, "")


# ------------------------------------------------------------------ VR-R4

def test_vr_r4_a_text_verdict_is_recorded_as_coming_from_the_text(w):
    w.fxw.queue(work(1))
    w.fxr.queue(says("VERDICT(approved): ok"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = settled(w, loop, WAIT)
    assert provenance(done) == {"text"}, done
    assert provenance(w.get(loop)) == {"text"}


def test_vr_r4_a_tool_verdict_is_recorded_as_coming_from_the_tool(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    done = settled(w, loop, WAIT)
    assert provenance(done) == {"tool"}, done


def test_vr_r4_the_verdict_transition_carries_the_provenance(w):
    w.fxw.queue(work(1), work(2))
    w.fxr.queue(says("VERDICT(rejected, 1): x"), verdict_entry("approved"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(3)
    settled(w, loop, WAIT_ROUNDS)
    events = verdict_events(w, loop)
    assert len(events) == 2, events
    assert [provenance(e) for e in events] == [{"text"}, {"tool"}]


def test_vr_r4_a_rejecting_text_verdict_is_visible_through_get_node_too(w):
    w.fxw.queue(work(1))
    w.fxr.queue(says("VERDICT(rejected, 1): x"))
    w.start_scheduler()
    loop, _, _ = w.mkloop(1)
    held = settled(w, loop, WAIT, "loop_max")
    assert provenance(held) == {"text"}, held


# ------------------------------------------------------------------ VR-R5

def hold_unresolved(w, rounds: int = 3, **reviewer_entry):
    """A loop held `unresolved_round` by a reviewer that wrote no verdict; the
    engine is stopped on return. Returns (loop, wk, rv, run id of the reviewer's run)."""
    w.fxw.queue(work(1), work(2))
    w.fxr.queue(says("Thorough review, but no verdict line.", **reviewer_entry))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(rounds)
    w.wait_held(loop, "unresolved_round", timeout=WAIT)
    run_id = w.get(rv)["runs"][-1]["run_id"]
    w.work_run = w.get(wk)["runs"][-1]["run_id"]
    w.stop_scheduler()
    return loop, wk, rv, run_id


def test_vr_r5_a_held_loop_whose_reviewer_wrote_an_approval_settles_at_the_next_start(w):
    loop, wk, rv, run_id = hold_unresolved(w)
    set_parsed_verdict(w, run_id, "approved")
    w.start_scheduler()
    done = settled(w, loop, WAIT, early=False)
    assert done["outcome"] == "approved"
    assert done["generations"][-1]["verdict"] == "approved"
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 1, "settling must not re-run anyone"
    kinds = w.transitions(loop)
    assert "verdict" in kinds and "loop_exited" in kinds
    assert provenance(done) == {"text"}


def test_vr_r5_a_held_loop_whose_reviewer_wrote_a_rejection_goes_to_the_next_round(w):
    loop, wk, rv, run_id = hold_unresolved(w)
    set_parsed_verdict(w, run_id, "rejected", defects=2)
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    done = settled(w, loop, WAIT_ROUNDS, early=False)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2
    assert "round_rejected" in w.transitions(loop)


def test_vr_r5_the_settle_happens_once_not_at_every_tick_or_restart(w):
    loop, wk, rv, run_id = hold_unresolved(w, rounds=1)
    set_parsed_verdict(w, run_id, "rejected", defects=1)
    w.start_scheduler()
    settled(w, loop, WAIT, "loop_max", early=False)
    w.quiet(1.5)                                    # several ticks
    w.restart_scheduler()
    w.quiet(1.5)
    kinds = w.transitions(loop)
    assert kinds.count("verdict") == 1 and kinds.count("round_rejected") == 1, kinds
    assert w.get(loop)["loop"]["rounds_rejected"] == 1


def test_vr_r5_a_held_loop_without_a_parsed_verdict_stays_held(w):
    loop, wk, rv, run_id = hold_unresolved(w)
    set_parsed_verdict(w, run_id, None)
    w.start_scheduler()
    held_stays(w, loop, "unresolved_round", (w.fxw, 1), (w.fxr, 1))
    assert "verdict" not in w.transitions(loop)


def test_vr_r5_a_held_loop_is_not_settled_from_another_runs_verdict(w):
    """The work child's run carries a parsed verdict; the reviewer's does not."""
    loop, wk, rv, _ = hold_unresolved(w)
    set_parsed_verdict(w, w.work_run, "approved")
    w.start_scheduler()
    held_stays(w, loop, "unresolved_round", (w.fxw, 1), (w.fxr, 1))


# ------------------------------------------------------------------ VR-R6

IMPLEMENT = {"spec_path": "context/specs/vr-unique-spec-path.md", "tests_task": "write tests",
             "implement_task": "implement it", "tester": "tester", "reviewer": "reviewer",
             "implementer": "implementer"}


@pytest.fixture
def tw(worlds):
    world = worlds("tw")
    for role in ("tester", "reviewer", "implementer"):
        world.provider(f"fx{role}")
        world.agent(role, f"fx{role}", writes=True)
    return world


def reviewer_tasks(w, top: str) -> list[str]:
    out = []

    def walk(node_id):
        node = w.get(node_id)
        if node["kind"] == "simple" and node["agent"] == "reviewer":
            out.append(node.get("task") or "")
        for c in node["children"]:
            walk(c)
    walk(top)
    return out


def test_vr_r6_the_implement_reviewers_are_told_to_call_give_verdict_and_given_the_spec_path(tw):
    tw.start_scheduler()
    top = tw.instantiate_ok("implement", IMPLEMENT)
    tasks = reviewer_tasks(tw, top)
    assert len(tasks) == 2
    for task in tasks:
        assert "give_verdict" in task, task
        assert IMPLEMENT["spec_path"] in task, task


def test_vr_r6_the_implement_reviewers_are_told_to_end_with_the_verdict_line(tw):
    tw.start_scheduler()
    top = tw.instantiate_ok("implement", IMPLEMENT)
    for task in reviewer_tasks(tw, top):
        assert "VERDICT(" in task, task


def test_vr_r6_the_review_loop_reviewer_is_told_to_call_give_verdict_and_end_with_the_line(tw):
    tw.start_scheduler()
    top = tw.instantiate_ok("review-loop", {"task": "do it", "worker": "implementer", "reviewer": "reviewer"})
    (task,) = reviewer_tasks(tw, top)
    assert "give_verdict" in task and "VERDICT(" in task, task


def test_vr_r6_the_shipped_reviewer_brief_names_the_tool():
    """A static check of the shipped brief (the contract names the brief itself)."""
    brief = Path(__file__).resolve().parents[1] / "src/multiagents/defaults/agents/team/reviewer.md"
    text = brief.read_text()
    assert "give_verdict" in text
    assert "VERDICT(approved)" in text and "VERDICT(rejected" in text
