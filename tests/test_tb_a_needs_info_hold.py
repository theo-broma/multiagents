"""TB-R1 (tooling batch 2026-10, package A): a node run whose final result ends
with a NEED_INFO marker holds its node (`held`, `hold.reason == "needs_info"`).

Black box over the scheduler RPC (`get_node`, `wait_for_nodes`, `merge_node`,
`close_node`) with fixture runs scripted by their final text.

Assumptions where the contract is silent (kept loose):
- the marker text is "recorded durably on the node": asserted as the marker
  strings appearing, in order, in the node's JSON view (any field) and still
  there after a scheduler restart. No field name is pinned.
- the `needs_info` transition is found in `wait_for_nodes(cursor=0)` by its
  name (`transition`/`kind`/... key, `node.` prefix dropped) and carries the
  marker text somewhere in its JSON.
- a refused merge is `ok: false` with code `not_done` (NC-R39, "as today").
- the bullet prefix (`- NEED_INFO(`) counts as line start, as for NEED_DECISION.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_fixture import m4_agent  # noqa: E402
from nc_fixture.m4_world import M4World, commit_entry, err_code, verdict_entry  # noqa: E402
from nc_harness import tname  # noqa: E402

WAIT = 8          # one launch / one transition
WAIT_LOOP = 15    # a loop round (worker + reviewer + integration)
QUIET = 2         # "nothing more happens" window (the scheduler ticks every second)


class MidTextProvider(m4_agent.M4Provider):
    """The M4 provider plus a `mid_text` directive: assistant text emitted
    during the run, before the final message."""

    def __init__(self, tmp, name, sock, **extra):
        super().__init__(tmp, name, sock, **extra)
        script = self.dir / "agent.py"
        text = m4_agent._SCRIPT.replace('"type": "text", "text": "working"',
                                        '"type": "text", "text": fx.get("mid_text", "working")')
        assert text != m4_agent._SCRIPT
        script.write_text(text.format(python=sys.executable, base=str(self.dir), sock=str(sock)))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


class World(M4World):
    def provider(self, name, **extra):
        fx = MidTextProvider(self.tmp, name, self.sock, **extra)
        self.providers[name] = fx
        return fx


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def node_with_text(w, text: str, tag: str = "N", **fields) -> str:
    return w.simple(tag, "wk", fx={"text": text, "write": {f"{tag}.txt": "x\n"},
                                   "commit": f"{tag} work"}, **fields)


def transitions(w, node_id: str) -> list[dict]:
    res = w.ok("wait_for_nodes", {"timeout": 0, "cursor": 0})
    return [t for t in res["transitions"] if node_id in json.dumps(t)]


def merge(w, node_id: str, force: bool = False) -> dict:
    return w.rpc("merge_node", {"id": node_id, "force": force})


MARK = "NEED_INFO(db): which database should the store use?"


# ------------------------------------------------------------------ the hold

def test_tb_r1_a_final_result_ending_in_need_info_holds_the_node(w):
    w.start_scheduler()
    n = node_with_text(w, f"I stopped early.\n{MARK}")
    held = w.wait_held(n, "needs_info", timeout=WAIT)
    assert held["state"] == "held"
    assert held["outcome"] is None, "a held node has no outcome yet"
    assert "which database should the store use?" in json.dumps(held)


def test_tb_r1_the_node_does_not_finish_while_held(w):
    w.start_scheduler()
    n = node_with_text(w, MARK)
    w.wait_held(n, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    got = w.get(n)
    assert got["state"] == "held" and got["outcome"] is None
    assert w.fxw.spawns() == 1, "the held node was launched again by itself"


def test_tb_r1_all_markers_are_recorded_in_order(w):
    w.start_scheduler()
    first, second = "NEED_INFO(a): FIRST-QUESTION-111?", "NEED_INFO(b): SECOND-QUESTION-222?"
    n = node_with_text(w, f"intro\n{first}\nsome prose\n{second}\n")
    held = w.wait_held(n, "needs_info", timeout=WAIT)
    blob = json.dumps(held)
    assert "FIRST-QUESTION-111" in blob and "SECOND-QUESTION-222" in blob
    assert blob.index("FIRST-QUESTION-111") < blob.index("SECOND-QUESTION-222")


def test_tb_r1_the_marker_text_survives_a_scheduler_restart(w):
    w.start_scheduler()
    n = node_with_text(w, "NEED_INFO(k): DURABLE-QUESTION-333?")
    w.wait_held(n, "needs_info", timeout=WAIT)
    w.restart_scheduler()
    again = w.get(n)
    assert again["state"] == "held" and (again["hold"] or {}).get("reason") == "needs_info"
    assert again["outcome"] is None
    assert "DURABLE-QUESTION-333" in json.dumps(again)
    w.quiet(QUIET)
    assert w.fxw.spawns() == 1


def test_tb_r1_a_needs_info_transition_with_the_text_reaches_wait_for_nodes(w):
    w.start_scheduler()
    n = node_with_text(w, "NEED_INFO(t): TRANSITION-QUESTION-444?")
    w.wait_held(n, "needs_info", timeout=WAIT)
    mine = [t for t in transitions(w, n) if tname(t) == "needs_info"]
    assert len(mine) == 1, [tname(t) for t in transitions(w, n)]
    assert "TRANSITION-QUESTION-444" in json.dumps(mine[0])
    assert "done" not in [tname(t) for t in transitions(w, n)]


# --------------------------------------------------------------- anchoring

@pytest.mark.parametrize("text", [
    "NEED_INFO(x): bare?",
    "   NEED_INFO(x): indented?",
    "\tNEED_INFO(x): tab-indented?",
    "- NEED_INFO(x): bulleted?",
    "work finished\n\n  NEED_INFO(x): after prose?\n",
])
def test_tb_r1_a_marker_at_line_start_holds(w, text):
    w.start_scheduler()
    n = node_with_text(w, text)
    w.wait_held(n, "needs_info", timeout=WAIT)


@pytest.mark.parametrize("text", [
    "The marker NEED_INFO(x): is only an example here.",
    "Use `NEED_INFO(x): question` to ask.",
    "`NEED_INFO(x): question`",
    "see:\n```\nNEED_INFO(x): inside a fence?\n```\ndone",
    "see:\n```text\nNEED_INFO(x): inside a fence?\n```\n",
    "NEED_INFO without a parenthesis is prose",
    "need_info(x): lower case is prose",
    "all done, nothing needed",
])
def test_tb_r1_quoted_midline_or_fenced_text_does_not_hold(w, text):
    w.start_scheduler()
    n = node_with_text(w, text)
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert (done.get("hold") or {}).get("reason") != "needs_info"


def test_tb_r1_an_ordinary_run_still_completes(w):
    w.start_scheduler()
    n = node_with_text(w, "finished the work")
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert "needs_info" not in w.transitions(n)


def test_tb_r1_need_info_in_the_middle_of_a_run_is_non_blocking(w):
    w.start_scheduler()
    n = w.simple("N", "wk", fx={"mid_text": "NEED_INFO(m): MIDRUN-QUESTION-555?",
                                "text": "I carried on and finished.",
                                "write": {"N.txt": "x\n"}, "commit": "N work"})
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert "needs_info" not in w.transitions(n)


def test_tb_r1_only_the_final_result_is_inspected_not_an_earlier_marker_it_moved_past(w):
    w.start_scheduler()
    n = w.simple("N", "wk", fx={"mid_text": "NEED_INFO(m): EARLY-QUESTION-666?",
                                "text": "answered it myself\nfinal answer: 42"})
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "completed"


# ------------------------------------------------------ verdict precedence

def test_tb_r1_a_verdict_takes_precedence_over_a_trailing_need_info(w):
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("approved", text="Looks fine.\nNEED_INFO(r): REVIEWER-QUESTION-777?"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = w.wait_state(loop, "done", timeout=WAIT_LOOP)
    assert done["outcome"] == "approved"
    r = w.get(rv)
    assert r["state"] == "done" and r["outcome"] == "approved"
    assert (r.get("hold") or {}).get("reason") != "needs_info"
    assert "needs_info" not in w.transitions(rv)


def test_tb_r1_a_rejecting_verdict_with_a_trailing_need_info_is_settled_by_the_verdict(w):
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("rejected", text="Not good.\nNEED_INFO(r): REVIEWER-QUESTION-888?"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(1)
    held = w.wait_held(loop, "loop_max", timeout=WAIT_LOOP)
    assert held["loop"]["rounds_rejected"] == 1
    assert (w.get(rv).get("hold") or {}).get("reason") != "needs_info"
    assert "needs_info" not in w.transitions(rv)


# --------------------------------------------------- dependency evaluation

def dep(node: str, require: str = "success") -> dict:
    return {"node": node, "require": require}


def test_tb_r1_a_dependent_requiring_success_never_launches_on_a_held_node(w):
    w.start_scheduler()
    a = node_with_text(w, MARK, tag="A")
    b = w.simple("B", "wk", depends_on=[dep(a)])
    w.wait_held(a, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    assert w.fxw.by_tag("B") == [], "a dependent launched on a node that asked for information"
    got = w.get(b)
    assert got["state"] == "open" and not got.get("outcome")


def test_tb_r1_the_dependent_is_not_failed_or_skipped_either(w):
    """Held is neither success nor failure: the dependent keeps waiting."""
    w.start_scheduler()
    a = node_with_text(w, MARK, tag="A")
    b = w.simple("B", "wk", depends_on=[dep(a)])
    w.wait_held(a, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    assert w.get(b)["state"] not in ("done", "cancelled", "held")


def test_tb_r1_closing_the_held_node_as_failed_settles_it_and_never_launches_a_success_dependent(w):
    w.start_scheduler()
    a = node_with_text(w, MARK, tag="A")
    b = w.simple("B", "wk", depends_on=[dep(a)])
    w.wait_held(a, "needs_info", timeout=WAIT)
    assert w.root_op("close_node", a, outcome="failed").get("ok") is True
    got = w.get(a)
    assert got["state"] == "done" and got["outcome"] == "failed"
    w.quiet(QUIET)
    assert w.fxw.by_tag("B") == []


# ------------------------------------------------------------- composites

def test_tb_r1_a_sequence_sees_the_child_as_held_not_completed(w):
    w.start_scheduler()
    a = node_with_text(w, MARK, tag="A")
    b = w.simple("B", "wk")
    seq = w.comp("sequence", [a, b])
    w.wait_held(a, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    assert w.fxw.by_tag("B") == [], "the sequence moved past a held child"
    s = w.get(seq)
    assert s["state"] != "done" and s["outcome"] is None
    assert w.get(b)["state"] == "open"


def test_tb_r1_a_sequence_that_has_finished_earlier_children_still_stops_at_the_held_one(w):
    w.start_scheduler()
    a = w.simple("A", "wk", fx={"text": "fine"})
    b = node_with_text(w, MARK, tag="B")
    c = w.simple("C", "wk")
    seq = w.comp("sequence", [a, b, c])
    w.wait_held(b, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    assert w.get(a)["outcome"] == "completed"
    assert w.fxw.by_tag("C") == []
    assert w.get(seq)["state"] != "done"


def test_tb_r1_a_loop_sees_a_held_worker_as_held_and_runs_no_review(w):
    w.fxw.queue({"text": MARK})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_held(wk, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    assert w.fxr.spawns() == 0, "the reviewer ran over a worker that asked for information"
    lp = w.get(loop)
    assert lp["state"] != "done" and lp["outcome"] is None
    assert lp["loop"]["rounds_rejected"] == 0


# ---------------------------------------------------------- merge / recovery

@pytest.mark.parametrize("force", [False, True])
def test_tb_r1_merge_refuses_a_held_node_with_not_done_force_or_not(w, force):
    w.start_scheduler()
    n = node_with_text(w, MARK)
    w.wait_held(n, "needs_info", timeout=WAIT)
    before = w.git("rev-parse", "HEAD").stdout
    reply = merge(w, n, force)
    assert reply.get("ok") is False and err_code(reply) == "not_done", reply
    assert w.git("rev-parse", "HEAD").stdout == before
    assert w.get(n)["state"] == "held"


def test_tb_r1_close_node_settles_a_held_node(w):
    w.start_scheduler()
    n = node_with_text(w, MARK)
    w.wait_held(n, "needs_info", timeout=WAIT)
    assert w.root_op("close_node", n, outcome="failed").get("ok") is True
    got = w.get(n)
    assert got["state"] == "done" and got["outcome"] == "failed"


def test_tb_r1_the_hold_is_written_once_for_one_run(w):
    w.start_scheduler()
    n = node_with_text(w, MARK)
    w.wait_held(n, "needs_info", timeout=WAIT)
    w.quiet(QUIET)
    w.restart_scheduler()
    w.quiet(1)
    assert w.transitions(n).count("needs_info") == 1


# ------------------------------------------------------------------- docs

def test_tb_r1_agent_facing_docs_say_need_info_as_last_word_holds_a_scheduled_node():
    """The preamble every subagent is given describes the two markers; its
    NEED_INFO paragraph must say a final NEED_INFO holds a scheduled node."""
    from multiagents import runner
    texts = [v for v in vars(runner).values()
             if isinstance(v, str) and "NEED_INFO(<topic>)" in v and "NEED_DECISION(<topic>)" in v]
    assert texts, "no agent-facing text describes the NEED_INFO and NEED_DECISION markers"
    for text in texts:
        start = text.index("NEED_INFO(<topic>)")
        para = text[start:text.index("NEED_DECISION(<topic>)", start)].lower()
        assert "hold" in para or "held" in para, para
        assert "last" in para or "final" in para, para
