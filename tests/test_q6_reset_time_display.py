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

import pytest

from multiagents.budget import reset_display


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
# the monitor display layer: the extras a usage script prints go through
# reset_display. C18 MQ-R2a: the scripts no longer print window lines (the core
# draws those, with the Q6 label, and test_c18_monitor_quota_panel.py pins
# them), but an extra that still carries an ISO stamp is rewritten the same way.
# ---------------------------------------------------------------------------

def _extras(monkeypatch, tmp_path, out: str) -> list[str]:
    """The lines under the windows for a provider whose usage script prints out."""
    from test_c18_monitor_quota_panel import Panel, reading, uniq

    name = uniq("q6")
    p = Panel(tmp_path, monkeypatch, {name: reading(name, None, known=False,
                                                    headroom=None)},
              run_action=lambda *a, **k: (0, out, ""))
    first = out.split()[0]
    row = p.settle(name, lambda r: any(first in l or "resets in" in l
                                       or "reset due" in l for l in r["lines"]))
    return row["lines"]


@pytest.mark.real_providers
def test_q6_extras_rewrite_iso_tokens(monkeypatch, tmp_path, tz):
    tz("Europe/Paris")
    soon = (datetime.now(timezone.utc) + timedelta(hours=1, minutes=5)
            ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    lines = _extras(monkeypatch, tmp_path, f"quota ok until {soon}  2013/2000 credits\n")
    shown = [l for l in lines if "credits" in l]
    assert len(shown) == 1, lines
    assert soon not in shown[0], shown[0]           # no raw UTC wall clock left
    assert "resets in 1h" in shown[0], shown[0]     # countdown leads
    local = datetime.fromisoformat(soon).astimezone().strftime("%H:%M")
    assert f"({local} " in shown[0], shown[0]       # viewer's clock, with zone
    assert "2013/2000 credits" in shown[0], shown[0]  # everything else survives


@pytest.mark.real_providers
def test_q6_extras_with_no_timestamp_pass_through_unchanged(monkeypatch, tmp_path):
    raw = "credits 0.00 of 85.00 \u2014 available"
    assert raw in _extras(monkeypatch, tmp_path, raw + "\n")


@pytest.mark.real_providers
def test_q6_extras_show_a_past_reset_as_due(monkeypatch, tmp_path, tz):
    tz("Europe/Paris")
    past = "2026-09-30T10:57:33+00:00"
    lines = _extras(monkeypatch, tmp_path, f"credits back at {past}\n")
    assert any("reset due" in l for l in lines), lines
    assert not any(past in l for l in lines), lines
