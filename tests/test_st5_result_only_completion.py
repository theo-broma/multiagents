"""ST5-R1/R2: result-only frames complete the matching open call."""

import pytest

from multiagents.providers import Event
from multiagents.supervisor import Supervisor


@pytest.mark.parametrize("identified", [True, False])
def test_result_without_terminal_state_completes_the_open_call(identified):
    sup = Supervisor(loop_repeats=3)
    for i in range(3):
        ident = str(i) if identified else ""
        assert sup.observe(Event(kind="tool", name="bash", args={"command": "poll"},
                                 state="in_progress", tool_id=ident)) is None
        assert "open tool" in sup.activity_evidence
        trip = sup.observe(Event(kind="step", tool_id=ident, result="fixed-digest"))
        assert sup.activity_evidence == ""
        if i < 2:
            assert trip is None
        else:
            assert trip and trip.reason == "doom_loop"


def test_result_without_terminal_state_does_not_close_an_unmatched_call():
    sup = Supervisor()
    sup.observe(Event(kind="tool", name="bash", state="in_progress", tool_id="open"))
    sup.observe(Event(kind="step", tool_id="other", result="fixed-digest"))
    assert "open tool" in sup.activity_evidence


def test_running_result_does_not_complete_an_open_call():
    sup = Supervisor()
    sup.observe(Event(kind="tool", name="bash", state="in_progress", tool_id="open"))
    sup.observe(Event(kind="step", state="running", tool_id="open", result="progress-digest"))
    assert "open tool" in sup.activity_evidence
