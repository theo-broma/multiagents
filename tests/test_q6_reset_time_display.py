"""Q6: the monitor shows a quota reset as countdown + local time, with the zone.

context/tickets/2026-09-30-unfiled.md, ticket 1: a viewer at UTC+2 read the
z.ai usage line's raw UTC ISO string as a local clock, decided a reset that
was still an hour away had already passed, and reported a quota that would not
come back. The contract that was already written for the generic path
(``budget.reset_label``) now holds for the script-rendered path too:

* the SCRIPT keeps printing UTC ISO (tests/test_zai_provider.py ZA-R4 holds
  that, and the machine data — budget JSON, ``resets_at`` — stays UTC);
* the monitor's DISPLAY of a usage line rewrites any ISO-8601 token through
  ``budget.reset_display``: countdown first, local clock with zone, e.g.
  ``resets in 1h05 (12:57 CEST)``; a past reset says ``reset due``;
* text that is not a timestamp is shown unchanged.

The zone is forced with the ``TZ`` environment variable and ``time.tzset()``,
so these assertions do not depend on where the suite happens to run.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from multiagents.budget import reset_display
from multiagents.monitor import snapshot

PATHS = SimpleNamespace(config=None)


@pytest.fixture
def tz(monkeypatch):
    """Force the process time zone, then put it back."""
    def set_tz(zone: str):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
    yield set_tz
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


# The ticket's own instant: 2026-09-30T10:57:33.809000+00:00, misread at
# UTC+2 as "10:57 local".
STAMP = "2026-09-30T10:57:33.809000+00:00"
NOW = datetime(2026, 9, 30, 9, 52, 33, tzinfo=timezone.utc).timestamp()


def test_q6_future_reset_is_countdown_then_local_time_with_zone(tz):
    tz("Europe/Paris")                     # CEST, UTC+2, on 2026-09-30
    out = reset_display(STAMP, now=NOW)
    assert out == "resets in 1h05 (12:57 CEST)", out


def test_q6_a_reset_time_in_the_past_says_due(tz):
    tz("Europe/Paris")
    late = datetime(2026, 9, 30, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    assert reset_display(STAMP, now=late) == "reset due (12:57 CEST)"


def test_q6_the_zone_follows_the_viewer_not_the_provider(tz):
    # Same instant, a different viewer: only the clock and zone move, the
    # countdown does not.
    tz("Pacific/Kiritimati")               # UTC+14, the far side of the line
    out = reset_display(STAMP, now=NOW)
    local = datetime.fromisoformat(STAMP).astimezone().strftime("%H:%M %Z")
    assert out == f"resets in 1h05 ({local})", out


def test_q6_unparsable_value_is_shown_unchanged():
    for text in ("not a timestamp", "2026-13-45T99:99:99+00:00", "pending"):
        assert reset_display(text) == text, text
    assert reset_display("") == ""
    assert reset_display(None) == ""


def test_q6_offset_without_colon_and_bare_z_both_parse(tz):
    tz("Europe/Paris")
    for stamp in ("2026-09-30T10:57:33Z", "2026-09-30T10:57:33+0000",
                  "2026-09-30T10:57:33+00:00"):
        assert reset_display(stamp, now=NOW).startswith("resets in 1h05"), stamp


def test_q6_naive_stamp_is_not_passed_off_as_local(tz):
    # No offset to trust: converting would invent an error, so it is shown as
    # sent and marked, on the precedent reset_label set.
    tz("Europe/Paris")
    assert reset_display("2026-09-30T10:57:33", now=NOW) == \
        "Sep 30 10:57 (no timezone)"


# ---------------------------------------------------------------------------
# the monitor display layer: script usage lines go through reset_display
# ---------------------------------------------------------------------------

def _script_usage(monkeypatch, out: str):
    from multiagents import scripts

    def fake_run_action(name, provider, executor, action, *args, **kwargs):
        assert action == "usage"
        return 0, out, ""

    monkeypatch.setattr(scripts, "run_action", fake_run_action)
    monkeypatch.setattr(snapshot, "_LINE_CACHE", {})
    return snapshot._usage_lines("q6-fake", object(), object(), {}, PATHS)


def test_q6_usage_lines_rewrite_iso_tokens(monkeypatch, tz):
    tz("Europe/Paris")
    soon = (datetime.now(timezone.utc) + timedelta(hours=1, minutes=5)
            ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    raw = f"five_hour ########## 100%  {soon}  2013/2000 credits"
    lines, source = _script_usage(monkeypatch, raw + "\n")
    assert source == "script"
    assert len(lines) == 1
    shown = lines[0]
    assert soon not in shown, shown              # no raw UTC wall clock left
    assert "resets in 1h" in shown, shown        # countdown leads
    local = datetime.fromisoformat(soon).astimezone().strftime("%H:%M")
    assert f"({local} " in shown, shown          # viewer's clock, with zone
    assert "2013/2000 credits" in shown, shown   # everything else survives


def test_q6_usage_lines_with_no_timestamp_pass_through_unchanged(monkeypatch):
    raw = "weekly   ####......  35%  plenty of window left"
    lines, source = _script_usage(monkeypatch, raw + "\n")
    assert (lines, source) == ([raw], "script")


def test_q6_usage_line_shows_a_past_reset_as_due(monkeypatch, tz):
    tz("Europe/Paris")
    past = "2026-09-30T10:57:33+00:00"
    lines, _ = _script_usage(
        monkeypatch, f"five_hour ########## 100%  {past}\n")
    assert "reset due" in lines[0], lines
