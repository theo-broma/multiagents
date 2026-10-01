"""CW — compaction while agents run, and the return checklist: CW-R1, R3, R4,
R5, R6, R7 (CW-R2, the safe stop, is in `test_cw_safe_point.py`).

Contract: `context/specs/cw-compact-while-waiting.md`. Written red, before any
implementation (ag-db8d42); it amends P0-R8c / P0-R8f and leans on SV-R11.

Black box. The harnesses are the ones the R8f and R8c suites already use:

- **Interactive** (`driver._run_supervised`): `Session` from
  `test_phase0_interactive_compact.py` — a real child "CLI" (a fake provider
  script), a real transcript, the real `scripts.exec_action`, the project's
  tree and events file. What is read: the fake's log (which actions ran, in what
  order, with what `MULTIAGENTS_*` environment — the relaunch's
  `MULTIAGENTS_RESUME_PROMPT` is the prompt R8f.4.4 says is absent), the
  driver's output, the events file, the tree.
- **Unattended** (`driver._supervise`): `Loop` from
  `test_phase0_unattended_compact.py`. The turn's prompt is the
  `MULTIAGENTS_NUDGE` the fake launch logs.

`CwSession` adds one thing to the interactive fake: its `compact` action can
edit the tree while it "runs", i.e. between the stop and the relaunch —
exactly where an adoption, a finish or an adoption failure happens in life.

What the contract fixes about the CW-R4 message, and so what is asserted: its
order (line 1, agent rows, unseen/questions/deferred/tickets, checklist), the
words it names (`finished during compaction`, `adopted`, `adoption failed:
<reason>`, `not yet adopted`, `+N more`), the tools in the checklist, the 4 000
character cap, and that it names no provider. What it does not fix, and is not
asserted: any other wording, punctuation, or how a row is laid out beyond one
agent per line.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402

from multiagents import driver  # noqa: E402
from multiagents.tree import Node, now as tree_now  # noqa: E402

import test_phase0_interactive_compact as ic  # noqa: E402
from test_phase0_interactive_compact import (  # noqa: E402,F401
    FIGURES,
    IDLE,
    KEEP,
    KEPT,
    OVER,
    SID,
    STOPPED_BY,
    UNDER,
    Session,
    announcements,
    assert_not_stopped,
    assert_stop_compact_resume,
    session,
    shows_seconds,
    stopped_then,
)
import test_phase0_unattended_compact as uc  # noqa: E402
from test_phase0_unattended_compact import (  # noqa: E402,F401
    COMPACT,
    LAUNCH,
    Loop,
    loop,
    productive,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")
LIMIT_CAP = [{"match": "spend cap reached", "resets": False, "detail": "spend cap"}]
MARKER = re.compile(r"compacted by the driver", re.IGNORECASE)
PROVIDER_WORDS = ("claude", "opencode", "agy", "fakeprov", "codex", "gemini")
CAP = 4_000


# ------------------------------------------------------------- harness --

EDITS = '''    for edit in c.get("edits", []):
        sys.path.insert(0, c["src"])
        from multiagents.tree import Tree
        _t = Tree(pathlib.Path(c["tree"]), pathlib.Path(c["events"]))
        if edit["op"] == "status":
            _t.set_status(edit["id"], edit["status"], edit.get("reason", ""))
        elif edit["op"] == "emit":
            _t.emit(edit["id"], edit["kind"], **edit.get("fields", {}))
'''


class CwSession(Session):
    """The R8f session whose `compact` action can change the tree."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        text = self.script.read_text()
        anchor = '    c = ctl.get("compact") or {}\n    if c.get("sleep"):'
        assert anchor in text, "the R8f fake changed shape; CwSession needs updating"
        self.script.write_text(text.replace(
            anchor, '    c = ctl.get("compact") or {}\n' + EDITS
            + '    if c.get("sleep"):', 1))

    def cw_run(self, launches=None, *, edits=None, probe: int = 0, **compact):
        compact.setdefault("exit", 0)
        compact.setdefault("stdout", FIGURES + "\n")
        compact.update({"edits": edits or [], "src": SRC,
                        "tree": str(self.paths.tree_file),
                        "events": str(self.paths.events_file)})
        return self.run(launches or stopped_then(), probe=probe, compact=compact)

    def prompt(self) -> str:
        """The prompt the relaunch was started with ('' when there is none)."""
        launches = self.calls("launch")
        assert len(launches) >= 2, f"no relaunch happened: {self.seq()}"
        return launches[1]["env"].get("MULTIAGENTS_RESUME_PROMPT", "")

    def nested(self, parent: str, status: str = "running") -> str:
        agent_id = f"ag-nst{len(self.tree.read()['nodes']):03d}"
        self.tree.add(Node(id=agent_id, agent="coder", provider="fakeprov", model="m",
                           parent=parent, depth=2, status=status, task="t",
                           session=SID))
        return agent_id


@pytest.fixture
def cw(tmp_path, monkeypatch, capsys):
    def build(**kwargs) -> CwSession:
        return CwSession(tmp_path, monkeypatch, capsys, **kwargs)
    return build


def settled(s: Session, status: str) -> str:
    """A node that ended and whose result was already seen: not in flight."""
    agent_id = s.finished(status)
    s.tree.mark_seen(agent_id)
    assert agent_id not in [n.id for n in s.tree.unseen(SID)]
    return agent_id


def set_status(agent_id: str, status: str, reason: str = "") -> dict:
    return {"op": "status", "id": agent_id, "status": status, "reason": reason}


def emit(agent_id: str, kind: str, **fields) -> dict:
    return {"op": "emit", "id": agent_id, "kind": kind, "fields": fields}


def row(message: str, agent_id: str) -> str:
    """The one line of the message that is about `agent_id` (the first)."""
    hits = [line for line in message.splitlines() if agent_id in line]
    assert hits, f"{agent_id} is not in the return message:\n{message}"
    return hits[0]


def rows(message: str, agent_id: str) -> list[str]:
    return [line for line in message.splitlines() if agent_id in line]


def first_line(message: str) -> str:
    return next(line for line in message.splitlines() if line.strip())


CHECKLIST = ["BRIEF.md", "agent_tree", "collect_agent", "list_questions",
             "wait_for_agents"]


def blocked(s_or_lp, path: str | None = None) -> list[dict]:
    events = (s_or_lp.events("compact_blocked") if hasattr(s_or_lp, "events")
              else [])
    return [e for e in events if path is None or e.get("path") == path]


# ================================================================== CW-R1 ==
# In-flight work no longer blocks. Interactive path first.

@pytest.mark.parametrize("status", ["running", "stuck", "pending", "detached",
                                    "awaiting_user"])
def test_cw_r1_interactive_a_root_agent_in_any_state_does_not_block(cw, status):
    s = cw()
    s.reading(OVER)
    s.node(status)
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_cw_r1_interactive_a_nested_agent_does_not_block(cw):
    s = cw()
    s.reading(OVER)
    parent = s.node("running")
    s.nested(parent)
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_cw_r1_interactive_everything_in_flight_at_once_does_not_block(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.node("stuck")
    s.node("awaiting_user")
    s.finished("done")                      # an unseen result
    s.finished("failed")                    # and another
    s.tree.defer({"agent": "coder", "task": "later"}, tree_now() + 3600, "quota")
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_cw_r1_interactive_work_that_appears_during_the_grace_period_does_not_cancel(cw):
    """It used to: a result landing between the announcement and the stop
    cancelled it. Now the stop goes ahead and nothing is cancelled."""
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")

    def finish_when_announced():
        deadline = time.time() + 10
        while time.time() < deadline:
            if s.events("compact_scheduled"):
                s.tree.set_status(agent_id, "done")
                return
            time.sleep(0.02)

    worker = threading.Thread(target=finish_when_announced, daemon=True)
    worker.start()
    s.run(stopped_then())
    worker.join(5)
    assert s.tree.get(agent_id).status == "done", "the scenario did not happen"
    assert s.events("compact_cancelled") == []
    assert_stop_compact_resume(s)


def test_cw_r1_interactive_the_other_conditions_still_hold_with_work_in_flight(cw):
    """Only the three blockers went. Under the threshold is still not due."""
    s = cw()
    s.reading(UNDER)
    s.node("running")
    s.finished("done")
    s.run(KEEP)
    assert_not_stopped(s)


def test_cw_r1_interactive_a_probe_that_says_no_still_stops_nothing_with_work_in_flight(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.run(KEEP, probe=1)
    assert_not_stopped(s)


def test_cw_r1_interactive_a_pending_usage_limit_still_wins_with_work_in_flight(cw):
    s = cw(limit_markers=LIMIT_CAP)
    ch.write_transcript(s.transcript, [ch.user("go"), ch.request(OVER),
                                       ch.limit_message("You have a spend cap reached.")])
    past = time.time() - 30
    os.utime(s.transcript, (past, past))
    s.node("running")
    code, _ = s.run([{"life": STOPPED_BY}])
    assert code == 3
    assert s.calls("compact") == []
    assert s.events("compact_scheduled") == []


def test_cw_r1_interactive_a_transcript_that_keeps_changing_is_still_not_at_rest(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.run([{"life": KEPT + 1, "append_every": IDLE / 3}])
    assert_not_stopped(s)


# ---- unattended

def add_node(lp: Loop, status: str, *, parent: str | None = None) -> str:
    agent_id = f"ag-{status[:3]}{len(lp.tree.read()['nodes']):03d}"
    lp.tree.add(Node(id=agent_id, agent="coder", provider="fakeprov", model="m",
                     parent=parent, depth=2 if parent else 1, status=status,
                     task="t", session=uc.SID))
    return agent_id


def add_unseen(lp: Loop, status: str = "done") -> str:
    agent_id = add_node(lp, "running")
    lp.tree.set_status(agent_id, status)
    assert [n.id for n in lp.tree.unseen(uc.SID)].count(agent_id) == 1
    return agent_id


@pytest.mark.parametrize("status", ["running", "stuck", "pending", "detached",
                                    "awaiting_user"])
def test_cw_r1_unattended_a_root_agent_in_any_state_does_not_block(loop, status):
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, status)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_cw_r1_unattended_a_nested_agent_does_not_block(loop):
    lp = loop()
    lp.reading(uc.OVER)
    parent = add_node(lp, "running")
    add_node(lp, "running", parent=parent)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


@pytest.mark.parametrize("status", ["done", "failed"])
def test_cw_r1_unattended_an_unseen_result_does_not_block(loop, status):
    lp = loop()
    lp.reading(uc.OVER)
    add_unseen(lp, status)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


@pytest.mark.parametrize("retry_in", [3600, -10])
def test_cw_r1_unattended_a_deferred_task_does_not_block_due_or_not(loop, retry_in):
    lp = loop()
    lp.reading(uc.OVER)
    lp.tree.defer({"agent": "coder", "task": "later"}, tree_now() + retry_in, "quota")
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_cw_r1_unattended_everything_in_flight_at_once_does_not_block(loop):
    lp = loop()
    lp.reading(uc.OVER)
    parent = add_node(lp, "running")
    add_node(lp, "running", parent=parent)
    add_node(lp, "stuck")
    add_node(lp, "awaiting_user")
    add_unseen(lp)
    lp.tree.defer({"agent": "coder", "task": "later"}, tree_now() + 3600, "quota")
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_cw_r1_unattended_an_agent_still_running_across_turns_compacts_each_time(loop):
    """The fake's compaction does not shrink the reading, so every qualifying
    turn compacts — with the agent running throughout."""
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, "running")
    lp.run([productive()], max_turns=3)
    assert lp.actions() == [LAUNCH, COMPACT] * 3


def test_cw_r1_unattended_the_other_conditions_still_hold_with_work_in_flight(loop):
    lp = loop()
    lp.reading(uc.UNDER)
    add_node(lp, "running")
    lp.run([productive()], max_turns=2)
    assert COMPACT not in lp.actions()


def test_cw_r1_unattended_never_after_a_failed_turn_even_with_work_in_flight(loop):
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, "running")
    lp.run([{"exit": 1}], max_turns=3)
    assert lp.actions() == [LAUNCH] * 3


def test_cw_r1_unattended_never_after_a_limited_turn_even_with_work_in_flight(loop):
    lp = loop(limit_markers=LIMIT_CAP)
    lp.reading(uc.OVER)
    add_node(lp, "running")
    code = lp.run([{**uc.PRODUCTIVE,
                    "append": [ch.limit_message("You have a spend cap reached.")]}],
                  max_turns=2)
    assert code == 3
    assert lp.actions() == [LAUNCH]


# ================================================================== CW-R3 ==

def test_cw_r3_nothing_running_keeps_the_old_words_and_reports_zero(cw):
    s = cw()
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1, out
    assert "nothing running" in lines[0], lines[0]
    assert shows_seconds(lines[0], ic.GRACE)
    scheduled = s.events("compact_scheduled")
    assert len(scheduled) == 1
    assert scheduled[0].get("tokens") == OVER
    assert scheduled[0].get("active") == 0
    assert type(scheduled[0].get("active")) is int


def test_cw_r3_the_line_counts_the_agents_running(cw):
    s = cw()
    s.reading(OVER)
    for _ in range(3):
        s.node("running")
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1, out
    assert re.search(r"(?<![\d,])3 agents running", lines[0]), lines[0]
    assert "nothing running" not in lines[0]
    assert shows_seconds(lines[0], ic.GRACE), "the grace period went missing"
    scheduled = s.events("compact_scheduled")
    assert len(scheduled) == 1
    assert scheduled[0].get("active") == 3 and scheduled[0].get("tokens") == OVER


def test_cw_r3_one_agent(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1 and re.search(r"(?<![\d,])1 agents? running", lines[0]), out
    assert s.events("compact_scheduled")[0].get("active") == 1


def test_cw_r3_only_agents_that_are_running_are_counted(cw):
    """Two running; a finished one with an unseen result and a merged one are
    not running, and the orchestrator's own driver node never counts."""
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.node("running")
    s.finished("done")
    settled(s, "merged")
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1 and re.search(r"(?<![\d,])2 agents running", lines[0]), out
    assert s.events("compact_scheduled")[0].get("active") == 2


def test_cw_r3_the_announcement_still_says_a_sent_message_cancels(cw):
    """R8f.3's cancel wording and cancel behaviour survive the new count."""
    s = cw()
    s.reading(OVER)
    s.node("running")
    code, out = s.run([{"life": STOPPED_BY, "on_scheduled": [ch.user("keep it")],
                        "then_life": IDLE / 2}])
    assert announcements(out)
    assert s.stops() == []
    assert len(s.events("compact_cancelled")) == 1
    assert code == 0


# ================================================================== CW-R4 ==
# The return message. Interactive: the relaunch's resume prompt.

def test_cw_r4_nothing_in_flight_resumes_with_no_prompt(cw):
    """R8f.4.4 unchanged: a merged agent, a seen result and open tickets are
    not in flight."""
    s = cw()
    s.reading(OVER)
    settled(s, "merged")
    s.see(s.finished("done"))
    s.tree.add_ticket("ag-x", "a bug", "body")
    s.cw_run()
    assert_stop_compact_resume(s)
    assert s.prompt() == "", "a prompt was sent although nothing was in flight"


@pytest.mark.parametrize("what", ["running", "stuck", "pending", "detached",
                                  "awaiting_user", "unseen_done", "unseen_failed",
                                  "deferred", "question"])
def test_cw_r4_anything_in_flight_resumes_with_a_prompt(cw, what):
    s = cw()
    s.reading(OVER)
    if what == "unseen_done":
        s.finished("done")
    elif what == "unseen_failed":
        s.finished("failed")
    elif what == "deferred":
        s.tree.defer({"agent": "coder", "task": "later"}, tree_now() + 3600, "quota")
    elif what == "question":
        # A parked question without a live agent: the question alone counts.
        s.tree.add_question(settled(s, "merged"), "topic", "which way?")
    else:
        s.node(what)
    s.cw_run()
    assert_stop_compact_resume(s)
    assert s.prompt().strip(), f"{what} was in flight and nothing was said on resume"


def test_cw_r4_the_first_line_says_when_and_how_much_and_what_did_not_survive(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.cw_run()
    line = first_line(s.prompt())
    assert MARKER.search(line), line
    assert re.search(r"\d{1,2}:\d{2}", line) and re.search(r"UTC|\bZ\b|\+00:00", line), (
        f"no UTC time in the first line: {line!r}")
    assert re.search(r"9,?000\s*(→|->|to)\s*9,?00\b", line), (
        f"the before → after figures are not in the first line: {line!r}")
    assert "wait" in line.lower(), (
        f"the first line does not say the interrupted waits did not survive: {line!r}")


def test_cw_r4_an_unavailable_figure_reads_unknown(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.cw_run(stdout="")
    assert "unknown" in first_line(s.prompt()).lower()


def test_cw_r4_a_failed_compaction_says_so_and_still_lists(cw):
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")
    s.cw_run(stopped_then({"life": KEPT}), exit=1,
             stderr="API Error: 529 overloaded\n", stdout="")
    message = s.prompt()
    line = first_line(message)
    assert "fail" in line.lower() and "529 overloaded" in line, line
    assert agent_id in message, "the failure line replaced the list instead of heading it"
    assert all(word in message for word in CHECKLIST), "the checklist is missing"


def test_cw_r4_an_unsupported_compaction_says_so_and_still_lists(cw):
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")
    s.cw_run(stopped_then({"life": KEPT}), exit=64, stdout="")
    message = s.prompt()
    assert not MARKER.search(first_line(message)), (
        "reports a compaction that did not happen")
    assert agent_id in message
    assert all(word in message for word in CHECKLIST)


def test_cw_r4_a_node_that_finished_during_the_compaction_is_labelled(cw):
    s = cw()
    s.reading(OVER)
    gone = s.node("running")
    quiet = s.node("running")
    s.cw_run(edits=[set_status(gone, "done")])
    message = s.prompt()
    line = row(message, gone)
    assert "finished during compaction" in line, line
    assert "running" in line and "done" in line, (
        f"the row gives neither the status at the stop nor the one now: {line!r}")
    assert "finished during compaction" not in row(message, quiet), (
        "an agent that did not change is labelled as having finished")


def test_cw_r4_a_node_that_failed_during_the_compaction_is_labelled(cw):
    s = cw()
    s.reading(OVER)
    gone = s.node("running")
    s.cw_run(edits=[set_status(gone, "failed", "the process ended")])
    assert "finished during compaction" in row(s.prompt(), gone)


def test_cw_r4_an_adopted_node_is_labelled(cw):
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")
    s.cw_run(edits=[set_status(agent_id, "detached"), emit(agent_id, "detached"),
                    set_status(agent_id, "running"), emit(agent_id, "adopted")])
    line = row(s.prompt(), agent_id)
    assert "adopted" in line and "not yet adopted" not in line
    assert "adoption failed" not in line


def test_cw_r4_a_node_not_yet_adopted_says_so(cw):
    """Adoption is asynchronous: still detached at relaunch."""
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")
    s.cw_run(edits=[set_status(agent_id, "detached"), emit(agent_id, "detached")])
    assert "not yet adopted" in row(s.prompt(), agent_id)


def test_cw_r4_a_failed_adoption_is_listed_with_its_reason(cw):
    s = cw()
    s.reading(OVER)
    agent_id = s.node("running")
    other = s.node("running")
    reason = "ValueError: command.json is corrupt"
    s.cw_run(edits=[
        set_status(agent_id, "detached"), emit(agent_id, "detached"),
        emit(agent_id, "adopt_failed", detail=reason),
        set_status(agent_id, "failed",
                   f"could not be adopted after its server exited: {reason}")])
    line = row(s.prompt(), agent_id)
    assert "adoption failed" in line and "command.json is corrupt" in line, line
    assert "adoption failed" not in row(s.prompt(), other)


def test_cw_r4_a_failed_adoption_is_never_silent_among_many(cw):
    s = cw()
    s.reading(OVER)
    ids = [s.node("running") for _ in range(5)]
    victim = ids[2]
    s.cw_run(edits=[
        emit(victim, "adopt_failed", detail="OSError: boom"),
        set_status(victim, "failed", "could not be adopted after its server "
                   "exited: OSError: boom")])
    message = s.prompt()
    assert "adoption failed" in row(message, victim)
    assert sum("adoption failed" in line for line in message.splitlines()) == 1


def test_cw_r4_merged_and_discarded_agents_are_not_listed(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    merged = settled(s, "merged")
    discarded = settled(s, "discarded")
    s.cw_run()
    message = s.prompt()
    assert merged not in message and discarded not in message


def test_cw_r4_driver_roles_are_not_listed(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.cw_run()
    assert "dr-f8f000" not in s.prompt(), "the orchestrator's own node is listed"


def test_cw_r4_unseen_results_are_listed_by_id_terminal_ones_included(cw):
    s = cw()
    s.reading(OVER)
    done = s.finished("done")
    failed = s.finished("failed")
    seen = s.finished("done")
    s.see(seen)
    s.node("running")
    s.cw_run()
    message = s.prompt()
    for agent_id in (done, failed):
        assert agent_id in message, f"unseen result {agent_id} is not listed"
    # The unseen section is the one that is not the agent rows: a result seen
    # since is in no unseen list, and its row (it is not merged) is a row.
    unseen_lines = [line for line in message.splitlines()
                    if "unseen" in line.lower()]
    assert unseen_lines and all(seen not in line for line in unseen_lines)


def test_cw_r4_a_result_that_arrived_during_the_compaction_is_unseen_and_listed(cw):
    s = cw()
    s.reading(OVER)
    gone = s.node("running")
    s.cw_run(edits=[set_status(gone, "done")])
    unseen_lines = [line for line in s.prompt().splitlines()
                    if "unseen" in line.lower()]
    assert any(gone in line for line in unseen_lines), unseen_lines


def test_cw_r4_parked_questions_are_listed_by_id(cw):
    s = cw()
    s.reading(OVER)
    asking = s.node("awaiting_user")
    q1 = s.tree.add_question(asking, "scope", "which way?")["id"]
    q2 = s.tree.add_question(asking, "naming", "what name?")["id"]
    answered = s.tree.add_question(asking, "old", "settled?")["id"]
    s.tree.answer_question(answered, "yes")
    s.cw_run()
    message = s.prompt()
    assert q1 in message and q2 in message
    assert answered not in message, "an answered question is still 'parked'"


def test_cw_r4_deferred_tasks_are_counted(cw):
    s = cw()
    s.reading(OVER)
    for i in range(3):
        s.tree.defer({"agent": "coder", "task": f"later {i}"},
                     tree_now() + 3600 * (i + 1), "quota")
    s.cw_run()
    lines = [line for line in s.prompt().splitlines() if "deferred" in line.lower()]
    assert lines and any(re.search(r"(?<![\d.])3(?![\d.])", line) for line in lines), (
        f"no line gives the number of deferred tasks (3): {lines}")


def test_cw_r4_open_tickets_are_counted(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    for i in range(4):
        s.tree.add_ticket("ag-x", f"bug {i}", "body")
    closed = s.tree.add_ticket("ag-x", "closed one", "body")
    s.tree.set_ticket_status(closed["id"], "fixed")
    s.cw_run()
    lines = [line for line in s.prompt().splitlines() if "ticket" in line.lower()]
    assert any(re.search(r"(?<![\d.])4(?![\d.])", line) for line in lines), (
        f"no line gives the number of open tickets (4): {lines}")


def test_cw_r4_the_checklist_comes_last_and_in_order(cw):
    s = cw()
    s.reading(OVER)
    running = s.node("running")
    done = s.finished("done")
    q = s.tree.add_question(s.node("awaiting_user"), "t", "q?")["id"]
    s.cw_run()
    message = s.prompt()
    assert MARKER.search(first_line(message))
    head = message.index("BRIEF.md")
    for what in (running, done, q):
        assert message.rindex(what) < head, (
            f"{what} appears after the checklist starts")
    at = head
    for word in CHECKLIST[1:]:
        found = message.find(word, at)
        assert found >= 0, f"{word} is not in the checklist after {CHECKLIST[CHECKLIST.index(word) - 1]}"
        at = found
    assert "steer_agent" in message
    assert re.search(r"never restart|do not restart|don't restart", message, re.IGNORECASE), (
        "the checklist does not forbid restarting a running agent")


def test_cw_r4_it_names_no_provider(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.finished("done")
    s.cw_run()
    message = s.prompt().lower()
    assert message
    for word in PROVIDER_WORDS:
        assert word not in message, f"the return message names {word!r}"


def test_cw_r4_a_message_that_fits_is_not_truncated(cw):
    s = cw()
    s.reading(OVER)
    for _ in range(5):
        s.node("running")
    s.cw_run()
    message = s.prompt()
    assert 0 < len(message) <= CAP
    assert not re.search(r"\+\d+ more", message)


def test_cw_r4_the_cap_holds_with_200_agents_and_the_agent_list_says_how_many_more(cw):
    s = cw()
    s.reading(OVER)
    ids = [s.node("running") for _ in range(200)]
    s.cw_run()
    message = s.prompt()
    assert len(message) <= CAP, f"{len(message)} characters"
    more = re.findall(r"\+(\d+) more", message)
    assert more, "200 agents fit in 4 000 characters without a '+N more'?"
    assert "agent_tree" in message[message.index("+" + more[0] + " more"):][:120], (
        "the truncated list does not point at agent_tree")
    shown = {i for i in ids if i in message}
    assert len(shown) + int(more[0]) == 200, (
        f"{len(shown)} listed + {more[0]} more is not 200")
    assert all(word in message for word in CHECKLIST), (
        "truncating the lists cut the checklist")
    assert MARKER.search(first_line(message))


def test_cw_r4_the_cap_holds_with_200_unseen_results(cw):
    s = cw()
    s.reading(OVER)
    for _ in range(200):
        s.finished("done")
    s.cw_run()
    message = s.prompt()
    assert len(message) <= CAP, f"{len(message)} characters"
    assert re.search(r"\+\d+ more", message)
    assert all(word in message for word in CHECKLIST)


def test_cw_r4_the_cap_holds_with_200_agents_200_unseen_questions_and_a_failed_compaction(cw):
    s = cw()
    s.reading(OVER)
    for _ in range(200):
        s.node("running")
    for _ in range(200):
        s.finished("done")
    asking = s.node("awaiting_user")
    for i in range(50):
        s.tree.add_question(asking, f"t{i}", "q?")
    for i in range(30):
        s.tree.defer({"agent": "coder", "task": f"t{i}"}, tree_now() + 60 + i, "quota")
    s.cw_run(stopped_then({"life": KEPT}), exit=1, stdout="",
             stderr="x" * 3000)
    message = s.prompt()
    assert len(message) <= CAP, f"{len(message)} characters"
    assert all(word in message for word in CHECKLIST)


def test_cw_r4_the_prompt_goes_to_the_relaunch_only(cw):
    """The first launch has none, and the relaunch is the resumed session."""
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.cw_run()
    launches = s.calls("launch")
    assert not launches[0]["env"].get("MULTIAGENTS_RESUME_PROMPT")
    assert launches[1]["env"].get("MULTIAGENTS_RESUME") == "1"
    assert launches[1]["env"].get("MULTIAGENTS_SESSION_ID") == SID


def test_cw_r4_the_snapshot_is_taken_at_the_stop_not_at_the_relaunch(cw):
    """An agent that started during the compaction (it cannot, but the tree is
    shared) is not 'running at the stop' and must not be labelled as one that
    finished: only what was in flight at the stop can have 'finished during
    compaction'. A row for the same agent that finished is labelled once."""
    s = cw()
    s.reading(OVER)
    gone = s.node("running")
    s.cw_run(edits=[set_status(gone, "done")])
    message = s.prompt()
    assert message.count("finished during compaction") == 1


# ---- unattended: prepended once to the next turn, persisted across an exit

def nudges(lp: Loop) -> list[str]:
    return [e["env"].get("MULTIAGENTS_NUDGE", "") for e in lp.fake.calls("launch")]


SHRINKS = [productive(), productive(1_000), productive(1_000)]


def test_cw_r4_unattended_the_next_turn_carries_the_message_once(loop):
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, "running")
    code = lp.run(SHRINKS, max_turns=3)
    assert code == 0
    first, second, third = nudges(lp)
    assert not MARKER.search(first), "delivered before the compaction"
    assert MARKER.search(second), "the turn after a compaction with work in flight got no message"
    assert len(MARKER.findall(second)) == 1
    assert not MARKER.search(third), "delivered a second time"
    # Prepended: the ordinary nudge is still there, after it.
    assert driver.NUDGE in second
    assert second.index(MARKER.search(second).group(0)) < second.index(driver.NUDGE)
    assert all(word in second for word in CHECKLIST)
    assert len(second) - len(driver.NUDGE) <= CAP + 200


def test_cw_r4_unattended_with_nothing_in_flight_the_prompt_is_unchanged(loop):
    lp = loop()
    lp.reading(uc.OVER)
    lp.run(SHRINKS, max_turns=3)
    assert COMPACT in lp.actions()
    for nudge in nudges(lp):
        assert nudge == driver.NUDGE, "a message was added with nothing in flight"


@pytest.mark.parametrize("what", ["running", "unseen", "deferred", "question"])
def test_cw_r4_unattended_each_kind_of_in_flight_work_triggers_it(loop, what):
    lp = loop()
    lp.reading(uc.OVER)
    if what == "running":
        add_node(lp, "running")
    elif what == "unseen":
        add_unseen(lp)
    elif what == "deferred":
        lp.tree.defer({"agent": "coder", "task": "later"}, tree_now() + 3600, "quota")
    else:
        lp.tree.add_question(add_unseen(lp, "done"), "t", "q?")
    lp.run(SHRINKS, max_turns=2)
    assert MARKER.search(nudges(lp)[1])


def test_cw_r4_unattended_a_compaction_that_failed_says_so(loop):
    lp = loop()
    lp.reading(uc.OVER)
    agent_id = add_node(lp, "running")
    lp.run([productive(), productive(1_000)], max_turns=2,
           compact={"exit": 1, "stderr": "API Error: 529 overloaded\n"})
    second = nudges(lp)[1]
    assert "529 overloaded" in second and agent_id in second
    assert not MARKER.search(first_line(second))
    assert driver.NUDGE in second


def test_cw_r4_unattended_a_turn_limit_exit_keeps_it_for_the_next_invocation(loop):
    """max_turns=1: the one turn compacts (the decision precedes the limit) and
    the run ends before any further turn. The message is persisted, delivered
    once on the next invocation — and not on the one after."""
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, "running")
    assert lp.run(SHRINKS, max_turns=1) == 0
    assert len(nudges(lp)) == 1 and not MARKER.search(nudges(lp)[0])

    importlib.reload(driver)    # a new process would have none of this one's memory
    assert lp.run(SHRINKS, max_turns=1) == 0
    assert len(nudges(lp)) == 2
    assert len(MARKER.findall(nudges(lp)[1])) == 1, "not delivered on the next invocation"
    assert driver.NUDGE in nudges(lp)[1]

    importlib.reload(driver)
    assert lp.run(SHRINKS, max_turns=1) == 0
    assert not MARKER.search(nudges(lp)[2]), "delivered twice"


def test_cw_r4_unattended_an_idle_turn_exit_keeps_it_for_the_next_invocation(loop):
    """The compaction after the second idle turn is followed by the idle-turn
    exit; nothing was left to deliver it to."""
    lp = loop()
    lp.reading(1_000)
    add_node(lp, "running")
    idle_then_over = [{"append": [ch.user("a"), ch.request(1_000)]},
                      {"append": [ch.user("b"), ch.request(uc.OVER)]},
                      {"append": [ch.user("c"), ch.request(1_000)]}]
    assert lp.run(idle_then_over, max_turns=5) == 0
    assert COMPACT in lp.actions()
    assert len(nudges(lp)) == 2, "the run should have ended on the second idle turn"

    importlib.reload(driver)
    lp.run(idle_then_over, max_turns=1)
    assert len(MARKER.findall(nudges(lp)[2])) == 1, (
        "the message was lost when the driver exited before the next turn")

    importlib.reload(driver)
    lp.run(idle_then_over, max_turns=1)
    assert not MARKER.search(nudges(lp)[3]), "delivered twice"


# ================================================================== CW-R5 ==

def test_cw_r5_a_stop_for_compaction_cancels_and_orphans_nothing(cw):
    s = cw()
    s.reading(OVER)
    ids = [s.node("running") for _ in range(3)]
    s.run(stopped_then())
    assert_stop_compact_resume(s)
    for agent_id in ids:
        assert s.tree.get(agent_id).status == "running", (
            f"{agent_id} was touched by the compaction stop")
    touched = [e for e in ch.events(s.paths.events_file, "status")
               if e.get("status") in ("cancelled", "orphaned", "failed")]
    assert touched == []


# ================================================================== CW-R6 ==

def test_cw_r6_a_compaction_that_proceeds_reports_nothing_blocked(cw):
    s = cw()
    s.reading(OVER)
    s.node("running")
    s.run(stopped_then())
    assert s.events("compact_blocked") == []


def test_cw_r6_under_the_threshold_reports_nothing(cw):
    s = cw()
    s.reading(UNDER)
    s.run(KEEP)
    assert s.events("compact_blocked") == []


def test_cw_r6_disabled_after_a_failed_compaction_is_reported_once(cw):
    s = cw()
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}), compact={"exit": 1, "stderr": "boom\n"})
    events = s.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["disabled"], (
        f"{len(s.seq())} polls, expected one `disabled` event, got {events}")
    assert events[0].get("path") == "interactive"


def test_cw_r6_already_this_crossing_after_a_success_is_reported_once(cw):
    """The fake's compaction does not shrink the transcript: the relaunched
    session is still over, at rest, and not proposed again (R8f.5)."""
    s = cw()
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}))
    events = s.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["already_this_crossing"], events
    assert events[0].get("path") == "interactive"


@pytest.mark.parametrize("exit_code", [1, 2])
def test_cw_r6_a_failed_probe_is_reported_with_its_exit_once(cw, exit_code):
    s = cw()
    s.reading(OVER)
    s.run(KEEP, probe=exit_code)
    events = s.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["probe_failed"], events
    assert events[0].get("probe_exit") == exit_code
    assert events[0].get("path") == "interactive"
    assert len(s.calls("probe")) == 1, "the probe was re-run for the same rest episode"


def test_cw_r6_a_pending_usage_limit_is_reported_once(cw):
    s = cw(limit_markers=LIMIT_CAP)
    ch.write_transcript(s.transcript, [ch.user("go"), ch.request(OVER),
                                       ch.limit_message("You have a spend cap reached.")])
    past = time.time() - 30
    os.utime(s.transcript, (past, past))
    code, _ = s.run([{"life": STOPPED_BY}])
    assert code == 3
    events = s.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["usage_limit_pending"], events


def test_cw_r6_the_usage_limit_outranks_disabled(cw):
    """After a failed compaction the run is `disabled`; if the relaunched
    session then shows a limit message, both hold and the first in the contract's
    order is the one reported."""
    s = cw(limit_markers=LIMIT_CAP)
    s.reading(OVER)
    code, _ = s.run(stopped_then({"life": KEPT, "appends": [
        [0.0, [ch.limit_message("You have a spend cap reached.")]]]}),
        compact={"exit": 1, "stderr": "boom\n"})
    assert code == 3
    reasons = [e.get("reason") for e in s.events("compact_blocked")]
    assert reasons == ["usage_limit_pending"], reasons


def test_cw_r6_not_at_rest_is_reported_only_after_ten_idle_periods(cw):
    idle = 0.3
    s = cw(limits={"compact_idle_seconds": idle})
    s.reading(OVER)
    s.run([{"life": 10 * idle + 2.0, "append_every": 0.1}])
    events = s.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["not_at_rest"], events
    assert events[0].get("path") == "interactive"
    launched = s.calls("launch")[0]["t"]
    assert events[0]["t"] - launched >= 10 * idle - 0.15, (
        "reported before it had held for ten times compact_idle_seconds")


def test_cw_r6_a_working_session_for_less_than_that_is_not_news(cw):
    idle = 0.3
    s = cw(limits={"compact_idle_seconds": idle})
    s.reading(OVER)
    s.run([{"life": 10 * idle - 1.0, "append_every": 0.1}])
    assert s.events("compact_blocked") == []


def test_cw_r6_a_session_that_comes_to_rest_and_compacts_reports_nothing(cw):
    """Not at rest for a few idle periods (below ten), then at rest: it
    proceeds and nothing was ever news."""
    s = cw()
    s.reading(OVER)
    s.run([{"life": STOPPED_BY, "appends": [[0.5, [ch.user("more")]]]}, {"life": 0}])
    assert_stop_compact_resume(s)
    assert s.events("compact_blocked") == []


def test_cw_r6_unattended_disabled_after_unsupported_is_reported_once_not_per_turn(loop):
    lp = loop()
    lp.reading(uc.OVER)
    lp.run([productive()], max_turns=4, compact={"exit": 64})
    events = lp.events("compact_blocked")
    assert [e.get("reason") for e in events] == ["disabled"], events
    assert events[0].get("path") == "unattended"
    assert len(lp.events("compact_unsupported")) == 1


def test_cw_r6_unattended_a_compaction_that_proceeds_reports_nothing(loop):
    lp = loop()
    lp.reading(uc.OVER)
    add_node(lp, "running")
    lp.run([productive()], max_turns=2)
    assert lp.events("compact_blocked") == []


def test_cw_r6_unattended_under_the_threshold_reports_nothing(loop):
    lp = loop()
    lp.reading(uc.UNDER)
    lp.run([productive()], max_turns=3)
    assert lp.events("compact_blocked") == []


# ================================================================== CW-R7 ==

def _section() -> str:
    from test_phase0_context_window import _orchestrator_prompt, _section as section
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        return section(_orchestrator_prompt(Path(tmp), "implement"),
                       "## Your own context window")


def test_cw_r7_the_brief_says_the_driver_may_compact_while_agents_run():
    text = " ".join(_section().lower().split())
    assert re.search(r"(while|with) (agents?|work|something)[^.]{0,40}running|"
                     r"compact[^.]{0,80}(while|even if|even when)[^.]{0,60}(running|in flight)",
                     text), (
        "the section does not say the driver may compact while agents are running")


def test_cw_r7_the_brief_says_to_write_who_you_wait_on_into_the_brief_before_waiting():
    text = _section()
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if "BRIEF.md" in p]
    assert any(re.search(r"before", p, re.IGNORECASE)
               and re.search(r"wait", p, re.IGNORECASE)
               and re.search(r"which agents|agents you (are|'re) waiting", p,
                             re.IGNORECASE)
               for p in paragraphs), (
        "no paragraph tells the orchestrator to record, in BRIEF.md and before a "
        "long wait, which agents it is waiting on and why")
    assert re.search(r"\bwhy\b", text, re.IGNORECASE)


def test_cw_r7_the_brief_says_to_follow_the_return_checklist_first():
    text = " ".join(_section().lower().split())
    assert "checklist" in text, "the return message's checklist is not mentioned"
    assert re.search(r"(first|before anything)", text)


@pytest.mark.parametrize("stale", [
    r"no agent is running", r"nothing (is )?running", r"with nothing running",
    r"only (with|when) no agents?", r"no agents? (is |are )?(running|active)",
])
def test_cw_r7_no_sentence_says_the_driver_compacts_only_with_nothing_running(stale):
    text = " ".join(_section().lower().split())
    assert not re.search(stale, text), (
        f"the section still says the driver compacts only with nothing running "
        f"({stale!r})")


# ================================================================== CW-R8 ==
# The compaction is told what to keep. ASSUMED INTERFACE (the contract does not
# name it): the focus text reaches the provider's `compact` action in the
# environment variable below. Everything else is the contract's.

FOCUS_ENV = "MULTIAGENTS_COMPACT_FOCUS"
FOCUS_MAX = 1_000
TODAY = ["-p", "/compact", "--resume", SID, "--output-format", "json"]
NASTY = "keep ids: \"a\" 'b' $HOME `id` $(id)\nsecond line\\n; rm -rf / *  --flag"


def claude_compact(tmp_path, focus: str | None, *, check: str = "0", record: bool = True):
    p = ic.Probe(tmp_path, "claude")
    p.session()
    if not record:
        p.fake.write_text("#!%s\nimport json, sys\n"
                          "open(%r, 'a').write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
                          "print('{}')\n" % (sys.executable, str(p.log)))
        p.fake.chmod(0o755)
    env = {"PATH": f"{p.bin}:/usr/bin:/bin", "HOME": str(p.home),
           "MULTIAGENTS_BIN": str(p.fake), "MULTIAGENTS_MODEL": "m",
           "MULTIAGENTS_PROVIDER": "claude", "MULTIAGENTS_LAUNCH_STATE": str(p.home),
           "MULTIAGENTS_COMPACT_CHECK": check, "MULTIAGENTS_SESSION_ID": SID}
    if focus is not None:
        env[FOCUS_ENV] = focus
    import subprocess
    result = subprocess.run(["sh", str(ch.PROVIDER_SCRIPTS / "claude.sh"), "compact"],
                            capture_output=True, text=True, cwd=p.cwd, env=env,
                            timeout=60)
    calls = ([json.loads(line)["argv"] for line in p.log.read_text().splitlines()
              if line] if p.log.is_file() else [])
    return result, calls


@pytest.mark.parametrize("focus", [None, ""])
def test_cw_r8_script_no_focus_is_todays_exact_invocation(tmp_path, focus):
    result, calls = claude_compact(tmp_path, focus)
    assert result.returncode == 0, result.stderr
    assert calls == [TODAY]


def test_cw_r8_script_a_plain_focus_is_one_argument_after_compact(tmp_path):
    result, calls = claude_compact(tmp_path, "keep the agent ids")
    assert result.returncode == 0, result.stderr
    assert calls == [["-p", "/compact keep the agent ids", "--resume", SID,
                      "--output-format", "json"]]


def test_cw_r8_script_spaces_quotes_dollar_backticks_and_newline_arrive_unaltered(tmp_path):
    result, calls = claude_compact(tmp_path, NASTY)
    assert result.returncode == 0, result.stderr
    assert calls == [["-p", "/compact " + NASTY, "--resume", SID,
                      "--output-format", "json"]], calls


def test_cw_r8_script_nothing_in_the_focus_is_executed_or_expanded(tmp_path):
    marker = tmp_path / "pwned"
    focus = f"$(touch {marker}) `touch {marker}`; touch {marker}"
    result, calls = claude_compact(tmp_path, focus)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert calls[0][1] == "/compact " + focus


def test_cw_r8_script_a_focus_starting_with_a_dash_is_still_the_prompt_argument(tmp_path):
    result, calls = claude_compact(tmp_path, "--resume other")
    assert result.returncode == 0, result.stderr
    assert calls[0][0] == "-p" and calls[0][1] == "/compact --resume other"
    assert calls[0][2:] == ["--resume", SID, "--output-format", "json"]


def test_cw_r8_script_a_focus_of_exactly_the_limit_is_kept_whole(tmp_path):
    focus = "k" * FOCUS_MAX
    result, calls = claude_compact(tmp_path, focus)
    assert result.returncode == 0, result.stderr
    assert calls[0][1] == "/compact " + focus


@pytest.mark.parametrize("length", [FOCUS_MAX + 1, 5_000])
def test_cw_r8_script_an_over_long_focus_is_truncated_never_an_error(tmp_path, length):
    focus = "".join(chr(97 + i % 26) for i in range(length))
    result, calls = claude_compact(tmp_path, focus)
    assert result.returncode == 0, f"a long focus is truncated, not an error: {result.stderr}"
    assert len(calls) == 1
    arg = calls[0][1]
    assert arg.startswith("/compact ")
    sent = arg[len("/compact "):]
    assert len(sent) == FOCUS_MAX and focus.startswith(sent), (
        f"expected the first {FOCUS_MAX} characters, got {len(sent)}")
    assert calls[0][2:] == ["--resume", SID, "--output-format", "json"]


def test_cw_r8_script_the_transcript_check_still_decides_success_with_a_focus(tmp_path):
    result, calls = claude_compact(tmp_path, "keep ids", record=False)
    assert calls, "the CLI did not run"
    assert result.returncode != 0, "exit 0 from the CLI is not a compaction"
    assert "no compaction was recorded" in result.stderr


def test_cw_r8_script_a_successful_compaction_still_reports_its_figures(tmp_path):
    result, _ = claude_compact(tmp_path, "keep ids")
    assert result.returncode == 0
    assert re.search(r"9,?000\D+9,?00\b", result.stdout), result.stdout


def test_cw_r8_script_the_probe_ignores_the_focus_and_runs_nothing(tmp_path):
    result, calls = claude_compact(tmp_path, "keep ids", check="1")
    assert result.returncode == 0
    assert calls == []


def test_cw_r8_script_the_focus_is_not_required_for_other_providers(tmp_path):
    """agy and opencode ignore it: still 'cannot' (64), CLI never run."""
    for script in ("agy", "opencode"):
        (tmp_path / script).mkdir()
        p = ic.Probe(tmp_path / script, script)
        import subprocess
        env = {"PATH": f"{p.bin}:/usr/bin:/bin", "HOME": str(p.home),
               "MULTIAGENTS_BIN": str(p.fake), "MULTIAGENTS_MODEL": "m",
               "MULTIAGENTS_PROVIDER": script, "MULTIAGENTS_LAUNCH_STATE": str(p.home),
               "MULTIAGENTS_COMPACT_CHECK": "0", FOCUS_ENV: "keep ids"}
        done = subprocess.run(["sh", str(ch.PROVIDER_SCRIPTS / f"{script}.sh"), "compact"],
                              capture_output=True, text=True, cwd=p.cwd, env=env,
                              timeout=60)
        assert done.returncode == 64 and not p.cli_ran()


def test_cw_r8_the_shipped_default_is_a_bounded_non_empty_string():
    import yaml
    limits = yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]
    focus = limits.get("compact_focus")
    assert isinstance(focus, str) and focus.strip(), "no shipped compact_focus"
    assert len(focus) <= FOCUS_MAX
    lowered = focus.lower()
    for word in ("agent", "brief.md"):
        assert word in lowered, f"the default focus does not mention {word!r}"


def _focus_of(call: dict) -> str:
    return call["env"].get(FOCUS_ENV, "")


def _shipped_focus() -> str:
    import yaml
    return yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]["compact_focus"]


def test_cw_r8_interactive_the_configured_focus_reaches_the_compact_action(cw):
    s = cw(limits={"compact_focus": NASTY})
    s.reading(OVER)
    s.run(stopped_then())
    compact = s.calls("compact")
    assert len(compact) == 1 and _focus_of(compact[0]) == NASTY


def test_cw_r8_interactive_an_unset_key_gets_the_shipped_default(cw):
    s = cw()
    s.reading(OVER)
    s.run(stopped_then())
    assert _focus_of(s.calls("compact")[0]) == _shipped_focus()


def test_cw_r8_interactive_an_empty_focus_means_none(cw):
    s = cw(limits={"compact_focus": ""})
    s.reading(OVER)
    s.run(stopped_then())
    assert _focus_of(s.calls("compact")[0]) == ""


def test_cw_r8_interactive_the_probe_is_not_the_compaction(cw):
    """The focus goes to the real call; the probe compacts nothing either way."""
    s = cw(limits={"compact_focus": "keep ids"})
    s.reading(OVER)
    s.run(stopped_then())
    assert len(s.calls("compact")) == 1 and s.calls("probe")


def test_cw_r8_unattended_the_configured_focus_reaches_the_compact_action(loop):
    lp = loop()
    lp.config.project["limits"]["compact_focus"] = NASTY
    lp.reading(uc.OVER)
    lp.run([productive()], max_turns=1)
    compact = lp.fake.calls("compact")
    assert len(compact) == 1 and _focus_of(compact[0]) == NASTY


def test_cw_r8_unattended_an_unset_key_gets_the_shipped_default(loop):
    lp = loop()
    lp.reading(uc.OVER)
    lp.run([productive()], max_turns=1)
    assert _focus_of(lp.fake.calls("compact")[0]) == _shipped_focus()


def test_cw_r8_unattended_an_empty_focus_means_none(loop):
    lp = loop()
    lp.config.project["limits"]["compact_focus"] = ""
    lp.reading(uc.OVER)
    lp.run([productive()], max_turns=1)
    assert _focus_of(lp.fake.calls("compact")[0]) == ""
