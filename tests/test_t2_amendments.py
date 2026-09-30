"""Amendment MT-R2a (2026-09-30, after the advisor): fences, exactly.

Contract: `context/specs/t2-multi-ticket.md`. Same seam as
`tests/test_t2_multi_ticket.py`: a finished message handed to
`Runner._file_ticket`, observed through the ticket queue only.

- A fence opens on a line starting with 3+ backticks or 3+ tildes.
- It closes on a later line of the same character, at least as long.
- An unclosed fence runs to the end of the message.

Assumptions: a fence line is judged by its first characters (no indentation
cases are tested, the contract is silent on them); closing lines in these tests
are bare fence characters, since trailing text on a closer is unspecified.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

from multiagents.config import AgentSpec  # noqa: E402


def _file(tmp_path, monkeypatch, text):
    r = h.make_runner(tmp_path, monkeypatch, agents={
        "bug-reporter": AgentSpec("bug-reporter", "p", "m")})
    r._file_ticket("ag-1", text)
    return r.tree.read()["tickets"]


def _titles(tickets):
    return [t["title"] for t in tickets]


REAL_BEFORE = "TICKET(minor): real before\nBEFORE-BODY\n"
REAL_AFTER = "TICKET(blocking): real after\nAFTER-BODY\n"


def _hidden(tmp_path, monkeypatch, fenced_block):
    """A real ticket, a block, a real ticket; the block's marker must not file."""
    tickets = _file(tmp_path, monkeypatch, REAL_BEFORE + fenced_block + REAL_AFTER)
    return tickets


@pytest.mark.parametrize("fence", ["```", "````", "`````", "~~~", "~~~~", "~~~~~~~~"])
def test_mt_r2a_a_fence_of_three_or_more_of_either_character_hides_its_marker(
        tmp_path, monkeypatch, fence):
    tickets = _hidden(tmp_path, monkeypatch,
                      f"{fence}\nTICKET(blocking): inside the fence\n{fence}\n")
    assert _titles(tickets) == ["real before", "real after"]
    assert "inside the fence" not in tickets[1]["title"]


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_mt_r2a_a_fence_with_an_info_string_hides_its_marker(tmp_path, monkeypatch, fence):
    tickets = _hidden(tmp_path, monkeypatch,
                      f"{fence}python\nTICKET(blocking): inside\n{fence}\n")
    assert _titles(tickets) == ["real before", "real after"]


@pytest.mark.parametrize("fence", ["``", "~~", "`", "~"])
def test_mt_r2a_fewer_than_three_characters_is_not_a_fence(tmp_path, monkeypatch, fence):
    tickets = _file(tmp_path, monkeypatch,
                    f"{fence}\nTICKET(blocking): not fenced after all\nNOT-FENCED-BODY\n")
    assert _titles(tickets) == ["not fenced after all"]


def test_mt_r2a_a_marker_after_a_closed_fence_is_real_again(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch, "~~~\nTICKET(minor): hidden\n~~~\n")
    assert _titles(tickets) == ["real before", "real after"]
    assert "AFTER-BODY" in tickets[1]["body"]


def test_mt_r2a_fenced_text_stays_in_the_body_of_the_ticket_it_sits_in(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch,
                    "TICKET(minor): only one\nLEAD\n~~~\nTICKET(blocking): quoted\nFENCED-LINE\n~~~\nTAIL\n")
    assert _titles(tickets) == ["only one"]
    assert "FENCED-LINE" in tickets[0]["body"] and "TAIL" in tickets[0]["body"]


# -- a longer fence is closed only by an equal or longer one ----------------

def test_mt_r2a_a_shorter_fence_line_does_not_close_a_longer_fence(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch,
                      "````\nTICKET(blocking): first inside\n```\n"
                      "TICKET(blocking): second inside, after a short fence line\n````\n")
    assert _titles(tickets) == ["real before", "real after"]


def test_mt_r2a_a_longer_closing_line_closes_the_fence(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch,
                      "```\nTICKET(blocking): inside\n`````\n")
    assert _titles(tickets) == ["real before", "real after"]


def test_mt_r2a_an_equal_closing_line_closes_the_fence(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch, "~~~~\nTICKET(blocking): inside\n~~~~\n")
    assert _titles(tickets) == ["real before", "real after"]


def test_mt_r2a_a_fence_of_the_other_character_does_not_close(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch,
                      "```\nTICKET(blocking): one\n~~~\nTICKET(blocking): two\n```\n")
    assert _titles(tickets) == ["real before", "real after"]


def test_mt_r2a_tildes_are_not_closed_by_backticks(tmp_path, monkeypatch):
    tickets = _hidden(tmp_path, monkeypatch,
                      "~~~\nTICKET(blocking): one\n```\nTICKET(blocking): two\n~~~\n")
    assert _titles(tickets) == ["real before", "real after"]


def test_mt_r2a_a_shorter_fence_line_inside_is_content_and_a_marker_after_the_real_close_files(
        tmp_path, monkeypatch):
    """The real close is the second four-tick line; the marker after it is real."""
    tickets = _file(tmp_path, monkeypatch,
                    "````\nTICKET(blocking): inside\n```\n````\n"
                    "TICKET(minor): after the close\nCLOSE-BODY\n")
    assert _titles(tickets) == ["after the close"]


# -- an unclosed fence swallows the rest ------------------------------------

@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_mt_r2a_an_unclosed_fence_hides_every_marker_after_it(tmp_path, monkeypatch, fence):
    tickets = _file(tmp_path, monkeypatch,
                    REAL_BEFORE + f"{fence}\nsome code\n"
                    "TICKET(blocking): swallowed one\nX\n"
                    "TICKET(minor): swallowed two\nY\n")
    assert _titles(tickets) == ["real before"]


def test_mt_r2a_text_after_an_unclosed_fence_stays_with_the_ticket_before_it(
        tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch,
                    "TICKET(minor): sole\nSOLE-BODY\n```\nUNCLOSED-TEXT\n"
                    "TICKET(blocking): swallowed\n")
    assert _titles(tickets) == ["sole"]
    assert "UNCLOSED-TEXT" in tickets[0]["body"]
    assert "swallowed" not in tickets[0]["title"]


def test_mt_r2a_a_too_short_closer_leaves_the_fence_unclosed(tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch,
                    REAL_BEFORE + "````\ncode\n```\nTICKET(blocking): still swallowed\nZ\n")
    assert _titles(tickets) == ["real before"]


def test_mt_r2a_a_marker_before_any_fence_is_unaffected_by_a_later_unclosed_one(
        tmp_path, monkeypatch):
    tickets = _file(tmp_path, monkeypatch,
                    "TICKET(minor): one\nONE-BODY\nTICKET(blocking): two\nTWO-BODY\n~~~\ntail\n")
    assert _titles(tickets) == ["one", "two"]
