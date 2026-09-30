"""Contract MT: one bug-reporter run can file several tickets (bug-ac396a).

The contract is `context/specs/t2-multi-ticket.md`, MT-R1 to MT-R5.

Observed only from outside the runner:

- the ticket queue (`tree.read()["tickets"]`) after a finished message is
  handed to `Runner._file_ticket`, the seam the existing ticket tests already
  use. Its RETURN value is deliberately not asserted, except that the existing
  tests keep passing (MT-R5);
- for MT-R4, a real fake-CLI run through `Runner.consult` and
  `Runner.collect`: the result fields and what the queue holds.

Nothing here knows how markers are found: a regex, a line scanner or a
markdown parser passes if the tickets come out right.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

from multiagents.config import AgentSpec  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bug_reporter_three_tickets.txt"
FENCE = "`" * 3


def _runner(tmp_path, monkeypatch):
    return h.make_runner(tmp_path, monkeypatch, agents={
        "bug-reporter": AgentSpec("bug-reporter", "p", "m")})


def _file(tmp_path, monkeypatch, text):
    """Hand a finished message to the runner; return the queue it produced."""
    r = _runner(tmp_path, monkeypatch)
    r._file_ticket("ag-1", text)
    return r.tree.read()["tickets"]


def _titles(tickets):
    return [t["title"] for t in tickets]


# --------------------------------------------------------------------------
# MT-R1: every real marker files a ticket
# --------------------------------------------------------------------------

THREE = (
    "Some reasoning first, which belongs to no ticket: PRELUDE-TEXT.\n"
    "TICKET(minor): alpha is misrendered\n"
    "ALPHA-BODY the first defect.\n"
    "TICKET(blocking): beta loses work\n"
    "BETA-BODY the second defect.\n"
    "PROPOSED_FIX:\n"
    "BETA-FIX check the return value.\n"
    "TICKET(minor): gamma routes wrongly\n"
    "GAMMA-BODY the third defect.\n"
    "PROPOSED_FIX:\n"
    "GAMMA-FIX rank unknown lower.\n"
)


def test_mt_r1_three_markers_file_three_tickets_in_order(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch, THREE)
    assert _titles(tickets) == ["alpha is misrendered", "beta loses work",
                                "gamma routes wrongly"]
    assert [t["severity"] for t in tickets] == ["minor", "blocking", "minor"]


def test_mt_r1_each_body_holds_only_its_own_section(tmp_path, monkeypatch):
    a, b, c = _file(tmp_path, monkeypatch, THREE)
    assert "ALPHA-BODY" in a["body"]
    assert "BETA-BODY" in b["body"]
    assert "GAMMA-BODY" in c["body"]
    for ticket, own in ((a, "ALPHA"), (b, "BETA"), (c, "GAMMA")):
        for other in {"ALPHA", "BETA", "GAMMA"} - {own}:
            assert other not in ticket["body"], (own, other)
            assert other not in ticket["proposed_fix"], (own, other)
        assert "PRELUDE-TEXT" not in ticket["body"]
        assert "TICKET(" not in ticket["body"]


def test_mt_r1_the_fix_split_applies_per_section(tmp_path, monkeypatch):
    a, b, c = _file(tmp_path, monkeypatch, THREE)
    assert a["proposed_fix"] == ""
    assert b["proposed_fix"].startswith("BETA-FIX")
    assert c["proposed_fix"].startswith("GAMMA-FIX")
    assert "BETA-FIX" not in b["body"] and "GAMMA-FIX" not in c["body"]


def test_mt_r1_the_last_section_runs_to_the_end_of_the_message(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch, THREE + "TRAILING-LINE\n")
    assert "TRAILING-LINE" in tickets[-1]["proposed_fix"]


def test_mt_r1_an_indented_marker_counts_and_a_mid_line_one_does_not(tmp_path, monkeypatch):
    text = ("   TICKET(minor): indented but real\n"
            "REAL-BODY and, mid-line, see TICKET(blocking): not a marker here\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["indented but real"]
    assert "not a marker here" in tickets[0]["body"]


def test_mt_r1_a_marker_with_an_unknown_severity_is_not_a_marker(tmp_path, monkeypatch):
    text = ("TICKET(minor): first\nFIRST-BODY\n"
            "TICKET(catastrophic): everything is on fire\nMORE-FIRST-BODY\n"
            "TICKET(blocking): second\nSECOND-BODY\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["first", "second"]
    assert "everything is on fire" in tickets[0]["body"]


def test_mt_r1_the_real_three_ticket_message_files_three_tickets(tmp_path, monkeypatch):
    """Run ag-3afd5a's final message, verbatim. Only the last ticket was filed."""
    tickets = _file(tmp_path, monkeypatch, FIXTURE.read_text())
    assert [t["severity"] for t in tickets] == ["minor", "blocking", "minor"]
    assert _titles(tickets) == [
        "Monitor shows z.ai quota reset times as raw UTC ISO strings, "
        "and a user misread one as local time",
        "A deferred task can be removed from the durable deferred queue "
        "with no event and no report",
        "Routing sent a start to a quota-exhausted provider (agy) "
        "and reported an unconfigured fallback",
    ]
    first, second, third = tickets
    assert "opencode.sh" in first["body"] and "reset_label" in first["body"]
    assert "resume_deferred" in second["body"] and "drop_deferred" in second["body"]
    assert "choose_provider" in third["body"] and "fallback_chain" in third["body"]
    # The message's own preamble is the agent's chatter, not part of a ticket.
    assert "I've written all three tickets" not in first["body"]
    # And no body carries a neighbour's text.
    assert "resume_deferred" not in first["body"] and "resume_deferred" not in third["body"]
    assert "choose_provider" not in first["body"] and "choose_provider" not in second["body"]
    assert "opencode.sh:128" not in second["body"] and "opencode.sh:128" not in third["body"]
    assert all("TICKET(" not in t["body"] for t in tickets)


# --------------------------------------------------------------------------
# MT-R2: quoted markers are not markers
# --------------------------------------------------------------------------

def test_mt_r2_a_monologue_that_quotes_the_rule_files_only_the_real_ticket(tmp_path, monkeypatch):
    text = (
        "The brief says to end with `TICKET(blocking): one-line summary`, so I will.\n"
        "> TICKET(blocking): a quoted rule, which is not a ticket\n"
        f"{FENCE}\nTICKET(blocking): an example inside a fence\n{FENCE}\n"
        "More reasoning about what went wrong.\n"
        "TICKET(minor): the real defect\n"
        "REAL-BODY.\n"
    )
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["the real defect"]
    assert tickets[0]["severity"] == "minor"
    assert "quoted rule" not in tickets[0]["body"]
    assert "example inside a fence" not in tickets[0]["body"]


def test_mt_r2_a_marker_inside_a_fenced_block_files_nothing(tmp_path, monkeypatch):
    text = f"Here is the format:\n{FENCE}\nTICKET(blocking): only an example\nbody\n{FENCE}\nDone.\n"
    assert _file(tmp_path, monkeypatch, text) == []


def test_mt_r2_a_fence_with_an_info_string_still_hides_its_marker(tmp_path, monkeypatch):
    text = f"{FENCE}text\nTICKET(blocking): only an example\n{FENCE}\n"
    assert _file(tmp_path, monkeypatch, text) == []


@pytest.mark.parametrize("line", [
    "> TICKET(blocking): quoted",
    ">TICKET(blocking): quoted",
    "   > TICKET(blocking): quoted",
    "> > TICKET(blocking): quoted",
], ids=["spaced", "tight", "indented", "nested"])
def test_mt_r2_a_marker_on_a_quoted_line_files_nothing(tmp_path, monkeypatch, line):
    assert _file(tmp_path, monkeypatch, f"The rule reads:\n{line}\nThat is all.\n") == []


def test_mt_r2_a_marker_inside_inline_backticks_files_nothing(tmp_path, monkeypatch):
    text = ("The brief says to end with `TICKET(blocking): one-line summary`.\n"
            "It also accepts `TICKET(minor): another`, on one line.\n")
    assert _file(tmp_path, monkeypatch, text) == []


def test_mt_r2_a_span_of_inline_code_that_wraps_onto_a_marker_line_is_ignored(tmp_path, monkeypatch):
    text = ("End your message with the line `\n"
            "TICKET(blocking): one-line summary`\n"
            "and nothing else.\n")
    assert _file(tmp_path, monkeypatch, text) == []


def test_mt_r2_a_real_marker_whose_title_uses_backticks_is_still_a_ticket(tmp_path, monkeypatch):
    """The false-positive guard: quoting must not be inferred from any backtick."""
    text = "TICKET(minor): `resume_deferred` drops an entry when `start()` refuses\nBODY\n"
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["`resume_deferred` drops an entry when `start()` refuses"]


def test_mt_r2_a_quoted_marker_inside_a_real_ticket_does_not_split_it(tmp_path, monkeypatch):
    text = ("TICKET(blocking): the brief's example is filed as a ticket\n"
            "BEFORE-QUOTE\n"
            "> TICKET(minor): quoted from the brief\n"
            f"{FENCE}\nTICKET(minor): fenced example\nFENCED-LINE\n{FENCE}\n"
            "AFTER-QUOTE\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["the brief's example is filed as a ticket"]
    assert "BEFORE-QUOTE" in tickets[0]["body"]
    assert "FENCED-LINE" in tickets[0]["body"]
    assert "AFTER-QUOTE" in tickets[0]["body"]


def test_mt_r2_real_markers_on_both_sides_of_a_fenced_one_file_two(tmp_path, monkeypatch):
    text = ("TICKET(minor): one\nONE-BODY\n"
            f"{FENCE}\nTICKET(blocking): fenced\n{FENCE}\n"
            "TICKET(minor): two\nTWO-BODY\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["one", "two"]
    assert "fenced" not in tickets[1]["body"]


# --------------------------------------------------------------------------
# MT-R3: an empty section is not a ticket; a duplicated title files once
# --------------------------------------------------------------------------

def test_mt_r3_an_empty_section_between_two_real_ones_files_two(tmp_path, monkeypatch):
    text = ("TICKET(minor): first\nFIRST-BODY\n"
            "TICKET(minor): empty in the middle\n"
            "TICKET(blocking): third\nTHIRD-BODY\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["first", "third"]
    assert "empty in the middle" not in tickets[0]["body"] + tickets[1]["body"]


def test_mt_r3_a_whitespace_only_section_is_empty(tmp_path, monkeypatch):
    text = ("TICKET(minor): first\nFIRST-BODY\n"
            "TICKET(minor): whitespace only\n   \n\t\n\n"
            "TICKET(minor): third\nTHIRD-BODY\n")
    assert _titles(_file(tmp_path, monkeypatch, text)) == ["first", "third"]


def test_mt_r3_a_trailing_empty_marker_files_nothing_extra(tmp_path, monkeypatch):
    text = "TICKET(minor): real\nREAL-BODY\nTICKET(minor): nothing under it\n"
    assert _titles(_file(tmp_path, monkeypatch, text)) == ["real"]


def test_mt_r3_no_body_but_a_fix_is_still_a_ticket(tmp_path, monkeypatch):
    """The drop rule needs BOTH an empty body and no fix."""
    text = ("TICKET(minor): only a fix\n"
            "PROPOSED_FIX:\nONLY-FIX do this.\n"
            "TICKET(minor): second\nSECOND-BODY\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert _titles(tickets) == ["only a fix", "second"]
    assert tickets[0]["proposed_fix"].startswith("ONLY-FIX")


def test_mt_r3_a_duplicated_title_files_one_ticket_and_the_later_wins(tmp_path, monkeypatch):
    text = ("TICKET(minor): same title\nEARLIER-BODY\n"
            "TICKET(minor): a different one\nOTHER-BODY\n"
            "TICKET(blocking): same title\nLATER-BODY\n"
            "PROPOSED_FIX:\nLATER-FIX do this.\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert sorted(_titles(tickets)) == ["a different one", "same title"]
    kept = next(t for t in tickets if t["title"] == "same title")
    assert "LATER-BODY" in kept["body"] and "EARLIER-BODY" not in kept["body"]
    assert kept["severity"] == "blocking"
    assert kept["proposed_fix"].startswith("LATER-FIX")


def test_mt_r3_three_copies_of_a_title_file_one(tmp_path, monkeypatch):
    text = "".join(f"TICKET(minor): repeated\nBODY-{n}\n" for n in (1, 2, 3))
    tickets = _file(tmp_path, monkeypatch, text)
    assert len(tickets) == 1
    assert "BODY-3" in tickets[0]["body"]
    assert "BODY-1" not in tickets[0]["body"] and "BODY-2" not in tickets[0]["body"]


# --------------------------------------------------------------------------
# MT-R4: where the tickets are reported (a real run, through a fake CLI)
# --------------------------------------------------------------------------

def _ids(entries):
    return [e["id"] if isinstance(e, dict) else e for e in entries]


def _run_conversation(tmp_path, monkeypatch, text):
    """One finished run whose final message is `text`. Returns
    (runner, consult_result)."""
    provider = h.fake_cli(tmp_path, "p", events=[
        {"type": "result", "subtype": "success", "result": text}])
    spec = AgentSpec("bug-reporter", "p", "m", conversational=True)
    r = h.make_runner(tmp_path, monkeypatch, agents={"bug-reporter": spec},
                      providers={"p": provider})
    result = asyncio.run(r.consult("bug-reporter", "report", timeout=60))
    return r, result


def _run_agent(tmp_path, monkeypatch, text):
    provider = h.fake_cli(tmp_path, "p", events=[
        {"type": "result", "subtype": "success", "result": text}])
    spec = AgentSpec("bug-reporter", "p", "m")
    r = h.make_runner(tmp_path, monkeypatch, agents={"bug-reporter": spec},
                      providers={"p": provider})

    async def go():
        started = await r.start("bug-reporter", "report")
        await asyncio.wait_for(r.runs[started["agent_id"]].done.wait(), timeout=30)
        return started["agent_id"]

    return r, asyncio.run(go())


def test_mt_r4_the_result_lists_every_filed_ticket(tmp_path, monkeypatch):
    r, result = _run_conversation(tmp_path, monkeypatch, THREE)
    queued = r.tree.read()["tickets"]
    assert len(queued) == 3
    assert _ids(result["tickets"]) == [t["id"] for t in queued]
    assert [e["title"] for e in result["tickets"]] == _titles(queued)


def test_mt_r4_ticket_stays_as_the_last_one(tmp_path, monkeypatch):
    r, result = _run_conversation(tmp_path, monkeypatch, THREE)
    queued = r.tree.read()["tickets"]
    assert result["ticket"]["id"] == queued[-1]["id"]
    assert result["ticket"]["title"] == "gamma routes wrongly"


def test_mt_r4_a_single_ticket_is_a_list_of_one_and_ticket_agrees(tmp_path, monkeypatch):
    r, result = _run_conversation(
        tmp_path, monkeypatch, "TICKET(minor): only one\nONLY-BODY\n")
    queued = r.tree.read()["tickets"]
    assert len(queued) == 1
    assert _ids(result["tickets"]) == [queued[0]["id"]]
    assert result["ticket"]["id"] == queued[0]["id"]


def test_mt_r4_a_run_with_no_ticket_reports_none(tmp_path, monkeypatch):
    r, result = _run_conversation(tmp_path, monkeypatch, "I looked; no bug.")
    assert r.tree.read()["tickets"] == []
    assert not result.get("tickets")
    assert not result.get("ticket")


def test_mt_r4_collect_shows_every_filed_ticket(tmp_path, monkeypatch):
    r, agent_id = _run_agent(tmp_path, monkeypatch, THREE)
    queued = r.tree.read()["tickets"]
    assert len(queued) == 3
    collected = r.collect(agent_id)
    assert _ids(collected["tickets"]) == [t["id"] for t in queued]


def test_mt_r4_a_three_ticket_run_exposes_three_distinct_ids(tmp_path, monkeypatch):
    r, agent_id = _run_agent(tmp_path, monkeypatch, THREE)
    ids = _ids(r.collect(agent_id)["tickets"])
    assert len(ids) == 3 and len(set(ids)) == 3
    assert {t["agent"] for t in r.tree.read()["tickets"]} == {agent_id}


def test_mt_r4_the_result_record_on_disk_is_not_altered(tmp_path, monkeypatch):
    """The transcript keeps the agent's whole message, all three markers."""
    import json
    r, agent_id = _run_agent(tmp_path, monkeypatch, THREE)
    saved = json.loads((r.paths.run_dir(agent_id) / "result.json").read_text())
    assert saved["text"].count("TICKET(") == 3


# --------------------------------------------------------------------------
# MT-R5: nothing else changes
# --------------------------------------------------------------------------

def test_mt_r5_a_single_marker_files_one_ticket_with_the_split_fix(tmp_path, monkeypatch):
    text = ("Reasoning that is not part of the ticket.\n"
            "TICKET(blocking): merge_agent reports success on an empty branch\n"
            "## What happened\n\nIt returned merged with no commits.\n"
            "PROPOSED_FIX:\nCheck commits_on() before reporting merged.\n")
    tickets = _file(tmp_path, monkeypatch, text)
    assert len(tickets) == 1
    t = tickets[0]
    assert t["severity"] == "blocking"
    assert t["title"] == "merge_agent reports success on an empty branch"
    assert "What happened" in t["body"]
    assert "not part of the ticket" not in t["body"]
    assert t["proposed_fix"].startswith("Check commits_on()")


def test_mt_r5_text_without_a_marker_files_nothing(tmp_path, monkeypatch):
    assert _file(tmp_path, monkeypatch, "I looked and found no bug.") == []
    assert _file(tmp_path, monkeypatch, "") == []


def test_mt_r5_an_unknown_severity_alone_files_nothing(tmp_path, monkeypatch):
    assert _file(tmp_path, monkeypatch, "TICKET(catastrophic): everything is on fire") == []


def test_mt_r5_a_single_ticket_is_still_depersonalised(tmp_path, monkeypatch):
    home = str(Path.home())
    tickets = _file(tmp_path, monkeypatch,
                    f"TICKET(minor): leaks a path\nSee {home}/private/notes.txt\n")
    assert len(tickets) == 1
    assert home not in tickets[0]["body"]


# --------------------------------------------------------------------------
# MT-R3a: a lone real marker files a ticket even with an empty body
# --------------------------------------------------------------------------

def test_mt_r3a_a_lone_marker_with_no_body_files_a_ticket(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch, "TICKET(blocking): title and nothing else")
    assert _titles(tickets) == ["title and nothing else"]
    assert tickets[0]["severity"] == "blocking"


def test_mt_r3a_a_lone_marker_with_a_whitespace_body_files_a_ticket(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch, "Some reasoning.\nTICKET(minor): bare\n   \n\n")
    assert _titles(tickets) == ["bare"]


def test_mt_r3a_quoted_markers_do_not_make_a_lone_marker_a_pair(tmp_path, monkeypatch):
    """Only REAL markers are counted, so the drop rule stays off."""
    text = ("The brief says `TICKET(blocking): one-line summary`.\n"
            "> TICKET(blocking): quoted\n"
            f"{FENCE}\nTICKET(blocking): fenced\n{FENCE}\n"
            "TICKET(minor): the only real one\n")
    assert _titles(_file(tmp_path, monkeypatch, text)) == ["the only real one"]


def test_mt_r3a_with_two_markers_the_empty_ones_are_still_dropped(tmp_path, monkeypatch):
    """The exception is for exactly one marker, not for the last empty one."""
    text = "TICKET(minor): first, empty\nTICKET(minor): second, empty\n"
    assert _file(tmp_path, monkeypatch, text) == []


# --------------------------------------------------------------------------
# MT-R1a: the headed form of PROPOSED_FIX
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "## PROPOSED_FIX",
    "## PROPOSED_FIX:",
    "# PROPOSED_FIX",
    "### PROPOSED_FIX:",
    "PROPOSED_FIX",
    "PROPOSED_FIX:",
    "  ## PROPOSED_FIX  ",
], ids=["h2", "h2-colon", "h1", "h3-colon", "bare", "bare-colon", "indented"])
def test_mt_r1a_every_form_of_the_fix_heading_splits_body_from_fix(tmp_path, monkeypatch, line):
    text = f"TICKET(minor): t\nTHE-BODY\n{line}\nTHE-FIX do this.\n"
    (ticket,) = _file(tmp_path, monkeypatch, text)
    assert "THE-BODY" in ticket["body"]
    assert "THE-FIX" not in ticket["body"] and "PROPOSED_FIX" not in ticket["body"]
    assert ticket["proposed_fix"].startswith("THE-FIX")


def test_mt_r1a_the_headed_split_applies_per_section(tmp_path, monkeypatch):
    text = ("TICKET(minor): one\nONE-BODY\n## PROPOSED_FIX\nONE-FIX a.\n"
            "TICKET(blocking): two\nTWO-BODY\n"
            "TICKET(minor): three\nTHREE-BODY\n## PROPOSED_FIX:\nTHREE-FIX c.\n")
    one, two, three = _file(tmp_path, monkeypatch, text)
    assert one["proposed_fix"].startswith("ONE-FIX") and "ONE-FIX" not in one["body"]
    assert two["proposed_fix"] == "" and "TWO-BODY" in two["body"]
    assert three["proposed_fix"].startswith("THREE-FIX")
    assert "THREE-BODY" in three["body"] and "THREE-FIX" not in three["body"]
    assert "ONE-FIX" not in two["body"] + three["body"] + two["proposed_fix"]


def test_mt_r1a_a_heading_that_merely_mentions_the_word_is_not_a_split(tmp_path, monkeypatch):
    """The line must CONSIST of PROPOSED_FIX; prose about it is body."""
    text = ("TICKET(minor): t\nBODY-A\n## PROPOSED_FIX for the parser is below\nBODY-B\n"
            "The PROPOSED_FIX: heading is documented.\n")
    (ticket,) = _file(tmp_path, monkeypatch, text)
    assert ticket["proposed_fix"] == ""
    assert "BODY-B" in ticket["body"]


def test_mt_r1a_a_fix_heading_inside_a_fence_does_not_split(tmp_path, monkeypatch):
    text = (f"TICKET(minor): t\nBODY-A\n{FENCE}\n## PROPOSED_FIX\nEXAMPLE-FIX\n{FENCE}\nBODY-B\n")
    (ticket,) = _file(tmp_path, monkeypatch, text)
    assert ticket["proposed_fix"] == ""
    assert "EXAMPLE-FIX" in ticket["body"] and "BODY-B" in ticket["body"]


def test_mt_r1a_on_the_real_message_every_fix_is_non_empty_and_out_of_the_body(
        tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch, FIXTURE.read_text())
    assert len(tickets) == 3
    for ticket in tickets:
        assert ticket["proposed_fix"].strip(), ticket["title"]
        assert "PROPOSED_FIX" not in ticket["body"], ticket["title"]
        assert ticket["proposed_fix"].strip() not in ticket["body"], ticket["title"]
    first, second, third = tickets
    assert "Presentation only" in first["proposed_fix"]
    assert "Treat a refusal as a non-restart" in second["proposed_fix"]
    assert "Chain scope" in third["proposed_fix"]
    assert "Presentation only" not in second["proposed_fix"] + third["proposed_fix"]
    assert "Chain scope" not in first["proposed_fix"] + second["proposed_fix"]
