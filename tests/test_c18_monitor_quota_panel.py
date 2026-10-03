"""C18: the monitor's quota panel shows every window, uniformly.

Contract: context/specs/c18-monitor-quota-panel.md, MQ-R1..R5 with the
revision section (MQ-R1a, R2a, R3a, R4a) overriding the earlier wording.
Test names carry the requirement id.

Black box at the panel model: ``snapshot.providers_view`` rows, and what the
curses ``Screen`` draws from them. The reading (``budget.read_all``), the
scripts (``scripts.run_action``) and the install probe (``manifest.probe``) are
patched with fixtures; nothing reads credentials and nothing starts docker.

ASSUMPTIONS the contract leaves open (each is a NEED_INFO in the run report):

* The window lines live in the row's ``lines`` list (windows first, then
  extras), because that is the one list the TUI and the page already render.
* What the marks look like is NOT pinned. "Marked" is tested differentially:
  a window's line must differ from the same window's line when it is not
  constraining / not uncounted. The bar is pinned only as ``█`` filled and
  ``░`` empty, the glyph set the generic view and the TUI header use, filling
  as the quota is spent.
* The five install states are looked for as the contract's own words in the
  row's serialised text, plus the "configured but not installed" alert.
* Tie-break on the name is ascending.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents.budget import Budget
from multiagents.config import Config
from multiagents.monitor import snapshot as snap
from multiagents.paths import ProjectPaths, shipped_defaults_dir
from multiagents.tree import Tree

import sys
sys.path.insert(0, str(Path(__file__).parent / "support"))
from test_zai_provider import (FIVE_MS, WEEK_MS, envelope, limit,  # noqa: F401
                               env, zai)

pytestmark = pytest.mark.real_providers

PROVIDERS_DIR = shipped_defaults_dir() / "providers"
FILLED, EMPTY = "█", "░"
DASH = "—"

_ids = itertools.count()


def uniq(prefix: str) -> str:
    """A provider name no other test used: the usage-line cache is keyed by it."""
    return f"{prefix}-c18-{next(_ids)}"


# --------------------------------------------------------------------------
# fixtures


@pytest.fixture
def paris(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def in_hours(hours: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    return when.replace(microsecond=0).isoformat()


def local_clock(stamp: str) -> str:
    return f"{datetime.fromisoformat(stamp).astimezone():%b %d %H:%M %Z}"


def reading(name: str, windows: dict | None = None, *, headroom: float | None = 0.5,
            known: bool = True, note: str = "", **kw) -> Budget:
    return Budget(provider=name, known=known, headroom=headroom, note=note,
                  severity="normal" if known else "unknown",
                  windows=windows or {}, **kw)


class Panel:
    """providers_view over a fixture reading, repeatable."""

    def __init__(self, tmp_path, monkeypatch, readings: dict, *, kind="local",
                 host_has=True, run_action=None, probe=None):
        import multiagents.executor as executor_mod
        import multiagents.manifest as manifest_mod
        import multiagents.scripts as scripts_mod

        self.paths = ProjectPaths(tmp_path)
        self.paths.ensure()
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        self.config = Config(project={"executor": {"kind": kind}}, providers={},
                             agents={}, models={}, instruction_dirs=[])
        self.readings = readings
        def present(n):
            return host_has.get(n, True) if isinstance(host_has, dict) else host_has

        providers = {n: SimpleNamespace(
            billing="plan", name=n,
            available=(lambda n=n: "/usr/bin/fake" if present(n) else None))
            for n in readings}
        monkeypatch.setattr(snap, "load_providers", lambda _: providers)
        monkeypatch.setattr(snap, "read_all", lambda *a, **k: dict(readings))
        monkeypatch.setattr(executor_mod, "executor_for",
                            lambda *a: (lambda name: SimpleNamespace(kind=kind)))
        if run_action is not None:
            monkeypatch.setattr(scripts_mod, "run_action", run_action)
        else:
            monkeypatch.setattr(scripts_mod, "run_action",
                                lambda *a, **k: (64, "", ""))
        if probe is not None:
            monkeypatch.setattr(manifest_mod, "probe", probe)

    def view(self, with_scripts=False) -> dict[str, dict]:
        rows = snap.providers_view(self.paths, self.config, self.tree,
                                   with_scripts=with_scripts)
        self.rows = rows
        return {row["name"]: row for row in rows}

    def settle(self, name, done, seconds=6.0):
        """Refresh until ``done(row)``: extras may lag by one refresh (R4a)."""
        end = time.time() + seconds
        while True:
            row = self.view(with_scripts=True)[name]
            if done(row) or time.time() > end:
                return row
            time.sleep(0.1)


@pytest.fixture
def panel(tmp_path, monkeypatch, paris):
    def make(readings, **kw):
        return Panel(tmp_path, monkeypatch, readings, **kw)
    return make


def window_lines(row, names):
    return [line for line in row["lines"] if any(n in line for n in names)]


def line_of(row, name):
    hits = [line for line in row["lines"] if name in line]
    assert len(hits) == 1, f"expected one line for {name!r}, got {row['lines']!r}"
    return hits[0]


def pct(value: int) -> re.Pattern:
    return re.compile(rf"(?<![\d.]){value}%")


def render(panel, windows: dict, **kw) -> dict[str, str]:
    """{window name: its line} for one provider with these windows."""
    name = uniq("p")
    row = panel({name: reading(name, windows, **kw)}).view()[name]
    return {w: line_of(row, w) for w in windows}


DECOY = "zz-decoy"


def marked(panel, windows: dict, which: str) -> bool:
    """Is ``which`` visibly the constraining window? Differential: its line
    against its own line once a counted 100% decoy has taken the title."""
    alone = render(panel, windows)[which]
    shadowed = render(panel, {**windows, DECOY: {"percent": 100.0}})[which]
    return alone != shadowed


# --------------------------------------------------------------------------
# MQ-R1 / MQ-R1a


SHAPES = {
    "claude": {"session": {"percent": 95.0, "resets_at": in_hours(2)},
               "weekly_all": {"percent": 30.0, "resets_at": in_hours(100)}},
    "agy": {"gemini-5h": {"percent": 8.0, "resets_at": in_hours(3), "counted": True},
            "gemini-weekly": {"percent": 20.0, "resets_at": in_hours(90), "counted": True},
            "3p-5h": {"percent": 100.0, "resets_at": in_hours(4), "counted": False},
            "3p-weekly": {"percent": 67.0, "resets_at": in_hours(95), "counted": False}},
    "codex": {"5h": {"percent": 12.0, "resets_at": in_hours(1)},
              "weekly": {"percent": 44.0, "resets_at": in_hours(120)}},
    "opencode": {"rolling": {"percent": 0.0, "resets_at": in_hours(1)},
                 "weekly": {"percent": 86.0, "resets_at": in_hours(70)},
                 "monthly": {"percent": 51.0, "resets_at": in_hours(400)}},
    "opencode-zai": {"five_hour": {"percent": 5.0, "resets_at": in_hours(2)},
                     "weekly": {"percent": 80.0, "resets_at": in_hours(150)}},
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_mq_r1_every_window_gets_one_line_with_name_bar_percent_and_reset(panel, shape):
    windows = SHAPES[shape]
    name = uniq(shape)
    row = panel({name: reading(name, windows)}).view()[name]
    assert len(row["lines"]) == len(windows), (
        "no scripts, so the lines are exactly the windows", row["lines"])
    for window, detail in windows.items():
        line = line_of(row, window)
        assert pct(round(detail["percent"])).search(line), line
        assert FILLED in line or EMPTY in line, f"no bar: {line!r}"
        assert local_clock(detail["resets_at"]) in line, line
        assert "in " in line.split(local_clock(detail["resets_at"]), 1)[1], (
            "the Q6 countdown, not just the clock")


def test_mq_r1_a_window_without_a_reset_time_shows_a_dash(panel):
    lines = render(panel, {"win-alpha": {"percent": 40.0}})
    assert DASH in lines["win-alpha"]


def test_mq_r1_the_bar_fills_as_the_quota_is_spent(panel):
    lines = render(panel, {"win-none": {"percent": 0.0}, "win-half": {"percent": 50.0},
                           "win-all": {"percent": 100.0}})
    assert FILLED not in lines["win-none"] and EMPTY in lines["win-none"]
    assert FILLED in lines["win-half"] and EMPTY in lines["win-half"]
    assert EMPTY not in lines["win-all"] and FILLED in lines["win-all"]


def test_mq_r1_an_uncounted_window_is_shown_and_marked(panel):
    windows = {"win-own": {"percent": 30.0}, "win-resold": {"percent": 20.0}}
    counted = render(panel, windows)
    flagged = render(panel, {**windows,
                             "win-resold": {"percent": 20.0, "counted": False}})
    assert pct(20).search(flagged["win-resold"]), "still shown with its number"
    assert flagged["win-resold"] != counted["win-resold"], "marked as not counted"
    assert flagged["win-own"] == counted["win-own"], "the counted one is untouched"


def test_mq_r1a_a_missing_counted_means_counted(panel):
    base = {DECOY: {"percent": 100.0}}
    implicit = render(panel, {**base, "win-a": {"percent": 20.0}})
    explicit = render(panel, {**base, "win-a": {"percent": 20.0, "counted": True}})
    uncounted = render(panel, {**base, "win-a": {"percent": 20.0, "counted": False}})
    assert implicit["win-a"] == explicit["win-a"] != uncounted["win-a"]


def test_mq_r1_exactly_one_window_is_marked_as_the_tightest(panel):
    windows = {"win-a": {"percent": 30.0}, "win-b": {"percent": 80.0},
               "win-c": {"percent": 55.0}}
    assert [w for w in windows if marked(panel, windows, w)] == ["win-b"]


def test_mq_r1_an_uncounted_window_is_never_the_tightest(panel):
    windows = {"win-own": {"percent": 30.0},
               "win-resold": {"percent": 99.0, "counted": False}}
    assert marked(panel, windows, "win-own") is True
    assert marked(panel, windows, "win-resold") is False, (
        "the uncounted line differs from its shadowed self only by the pool mark")
    # ... which is the same line it has when it is plainly not the tightest:
    solo = render(panel, {"win-resold": {"percent": 99.0, "counted": False}})
    assert solo["win-resold"] == render(panel, windows)["win-resold"]


def test_mq_r1a_with_every_window_uncounted_none_is_the_tightest(panel):
    windows = {"win-a": {"percent": 30.0, "counted": False},
               "win-b": {"percent": 80.0, "counted": False}}
    assert not marked(panel, windows, "win-a") and not marked(panel, windows, "win-b")


def test_mq_r1a_a_tie_goes_to_the_earliest_reset(panel):
    late, early = in_hours(90), in_hours(5)
    windows = {"win-a": {"percent": 60.0, "resets_at": late},
               "win-b": {"percent": 60.0, "resets_at": early}}
    assert marked(panel, windows, "win-b") and not marked(panel, windows, "win-a")


def test_mq_r1a_a_tie_on_percent_and_reset_goes_to_the_name(panel):
    same = in_hours(5)
    windows = {"win-b": {"percent": 60.0, "resets_at": same},
               "win-a": {"percent": 60.0, "resets_at": same}}
    assert marked(panel, windows, "win-a") and not marked(panel, windows, "win-b")


def test_mq_r1a_the_tightest_is_decided_on_the_displayed_percent(panel):
    """A window that gives only a headroom is 100*(1-headroom) used, the
    spelling budget._window_used converts, and it can be the constraining one."""
    windows = {"win-pct": {"percent": 80.0}, "win-room": {"headroom": 0.1}}
    assert marked(panel, windows, "win-room") and not marked(panel, windows, "win-pct")


def test_mq_r1a_percent_beats_used_percent_beats_headroom(panel):
    lines = render(panel, {
        "win-all3": {"percent": 31.0, "used_percent": 82.0, "headroom": 0.9},
        "win-legacy": {"used_percent": 42.0, "headroom": 0.9},
        "win-room": {"headroom": 0.25},
    })
    assert pct(31).search(lines["win-all3"]) and not pct(82).search(lines["win-all3"])
    assert pct(42).search(lines["win-legacy"])
    assert pct(75).search(lines["win-room"]), lines["win-room"]


def test_mq_r1a_a_window_with_no_percent_shows_a_dash_and_no_bar(panel):
    windows = {"win-blank": {"resets_at": in_hours(3)}, "win-ok": {"percent": 40.0}}
    lines = render(panel, windows)
    assert DASH in lines["win-blank"]
    assert FILLED not in lines["win-blank"] and EMPTY not in lines["win-blank"]
    assert local_clock(windows["win-blank"]["resets_at"]) in lines["win-blank"]


def test_mq_r1a_a_window_with_no_percent_is_never_the_tightest(panel):
    only = {"win-blank": {"resets_at": in_hours(3)}}
    assert not marked(panel, only, "win-blank")
    both = {**only, "win-ok": {"percent": 1.0}}
    assert marked(panel, both, "win-ok")
    assert render(panel, both)["win-blank"] == render(panel, only)["win-blank"]


def test_mq_r1a_the_account_is_shown_with_its_window(panel):
    lines = render(panel, {"win-a": {"percent": 10.0, "account": "alice@example.org"},
                           "win-b": {"percent": 10.0}})
    assert "alice@example.org" in lines["win-a"]
    assert "alice@example.org" not in lines["win-b"]


def test_mq_r1a_a_known_reading_without_windows_shows_one_overall_line(panel):
    name = uniq("solo")
    row = panel({name: reading(name, None, headroom=0.14)}).view()[name]
    assert len(row["lines"]) == 1, row["lines"]
    line = row["lines"][0]
    assert "overall" in line and pct(86).search(line)
    assert FILLED in line and EMPTY in line


def test_mq_r1a_there_is_no_overall_line_when_windows_exist(panel):
    name = uniq("multi")
    row = panel({name: reading(name, {"win-a": {"percent": 10.0}})}).view()[name]
    assert len(row["lines"]) == 1 and "win-a" in row["lines"][0]
    assert not any("overall" in line for line in row["lines"])


def test_mq_r1a_an_unknown_reading_without_windows_shows_no_bar(panel):
    name = uniq("unk")
    row = panel({name: reading(name, None, known=False, headroom=None,
                               note="no quota surface")}).view()[name]
    assert not any(FILLED in l or EMPTY in l for l in row["lines"]), row["lines"]


def test_mq_r1a_with_scripts_off_the_window_lines_are_still_produced(panel):
    windows = SHAPES["agy"]
    name = uniq("agy")
    rows = panel({name: reading(name, windows)})
    row = rows.view(with_scripts=False)[name]
    assert len(window_lines(row, windows)) == len(windows)
    assert row["budget"]["windows"].keys() == windows.keys(), "the reading is kept"


def test_mq_r1a_many_windows_are_all_in_the_model_and_all_on_the_tui(panel, monkeypatch):
    import curses
    from multiagents.monitor.tui import Screen

    windows = {f"win-{i:02d}": {"percent": float(i * 7)} for i in range(9)}
    name = uniq("many")
    row = panel({name: reading(name, windows)}).view()[name]
    assert len(row["lines"]) == 9

    class Window:
        lines: dict = {}

        def getmaxyx(self):
            return (80, 200)

        def addnstr(self, y, x, text, n, attr=0):
            line = self.lines.get(y, "")
            line = line.ljust(x) if len(line) < x else line
            self.lines[y] = line[:x] + text[:n] + line[x + len(text[:n]):]

    window = Window()
    window.lines = {}
    monkeypatch.setattr(curses, "color_pair", lambda n: 0)
    screen = Screen(window, None)
    screen.state = {"providers": [row], "at": time.time()}
    screen.draw_live(0)
    drawn = "\n".join(window.lines[y] for y in sorted(window.lines))
    for window_name in windows:
        assert window_name in drawn, f"{window_name} was cut from the TUI:\n{drawn}"


# --------------------------------------------------------------------------
# MQ-R2 / MQ-R2a


def scripted(output: str, code: int = 0, stderr: str = ""):
    return lambda *a, **k: (code, output, stderr)


def test_mq_r2_two_providers_whose_scripts_print_different_glyphs_share_one_format(
        panel):
    windows = {"win-a": {"percent": 50.0, "resets_at": in_hours(5)},
               "win-b": {"percent": 20.0}}
    one, two = uniq("hash"), uniq("block")

    def run(name, provider, executor, action, *a, **k):
        if name == one:
            return 0, "####......  50% of the tightest window\n", ""
        return 0, "\u2588\u2588\u2588\u2588\u2588\u2591\u2591\u2591\u2591\u2591  50% used\n", ""

    p = panel({one: reading(one, windows), two: reading(two, windows)},
              run_action=run)
    p.settle(one, lambda r: len(r["lines"]) > 2)
    p.settle(two, lambda r: len(r["lines"]) > 2)
    rows = p.view(with_scripts=True)
    for window in windows:
        mine, theirs = line_of(rows[one], window), line_of(rows[two], window)
        assert mine == theirs, "one format for every provider"
        assert "#" not in mine


def test_mq_r2_the_windows_come_from_the_reading_not_from_the_script(panel):
    windows = {"win-a": {"percent": 50.0}, "win-b": {"percent": 20.0}}
    name = uniq("p")
    p = panel({name: reading(name, windows)},
              run_action=scripted("totally unrelated words\n"))
    row = p.settle(name, lambda r: "totally unrelated words" in r["lines"])
    assert len(window_lines(row, windows)) == 2, row["lines"]
    assert "totally unrelated words" in row["lines"], "extras are appended"


def test_mq_r2_extras_come_after_every_window_line(panel):
    windows = {"win-a": {"percent": 50.0}, "win-b": {"percent": 20.0}}
    name = uniq("p")
    p = panel({name: reading(name, windows)},
              run_action=scripted("credits 0.00 of 85.00 — available\n"))
    row = p.settle(name, lambda r: any("credits" in l for l in r["lines"]))
    lines = row["lines"]
    assert len(window_lines(row, windows)) == 2, lines
    extra = next(i for i, l in enumerate(lines) if "credits" in l)
    assert all(i < extra for i, l in enumerate(lines) if "win-" in l), lines


def test_mq_r2_a_claude_credits_extra_survives(panel):
    windows = SHAPES["claude"]
    name = uniq("claude")
    p = panel({name: reading(name, windows)},
              run_action=scripted("credits 0.00 of 85.00 — available\n"))
    row = p.settle(name, lambda r: any("credits" in l for l in r["lines"]))
    assert any("credits 0.00 of 85.00" in l for l in row["lines"]), row["lines"]
    assert len(window_lines(row, windows)) == 2


def test_mq_r2_a_zai_used_over_limit_credits_extra_survives(panel):
    windows = SHAPES["opencode-zai"]
    name = uniq("zai")
    p = panel({name: reading(name, windows)},
              run_action=scripted("9963/10000 credits\n"))
    row = p.settle(name, lambda r: any("9963/10000" in l for l in r["lines"]))
    assert any("9963/10000 credits" in l for l in row["lines"]), row["lines"]


def test_mq_r2_a_provider_without_windows_shows_its_note_and_no_bars(panel):
    name = uniq("deepinfra")
    p = panel({name: reading(name, None, known=False, headroom=None)},
              run_action=scripted("metered billing: pay per token\n"))
    row = p.settle(name, lambda r: "metered billing: pay per token" in r["lines"])
    assert "metered billing: pay per token" in row["lines"], row["lines"]
    assert not any(FILLED in l or EMPTY in l for l in row["lines"])


def test_mq_r2_a_provider_without_windows_whose_script_defers_shows_the_reading_note(
        panel):
    name = uniq("deepinfra")
    p = panel({name: reading(name, None, known=False, headroom=None,
                             note="metered per token; no quota surface")})
    row = p.view(with_scripts=True)[name]
    row = p.settle(name, lambda r: bool(r["lines"]))
    assert any("metered per token" in l for l in row["lines"]), row["lines"]
    assert not any(FILLED in l or EMPTY in l for l in row["lines"])


def test_mq_r2a_exit_64_is_quiet(panel):
    windows = {"win-a": {"percent": 50.0}, "win-b": {"percent": 20.0}}
    name = uniq("p")
    p = panel({name: reading(name, windows)}, run_action=scripted("", 64))
    for _ in range(4):
        row = p.view(with_scripts=True)[name]
        time.sleep(0.15)
    assert len(row["lines"]) == 2, ("windows only, no diagnostic", row["lines"])


@pytest.mark.parametrize("code", [1, 2, 124, 127])
def test_mq_r2a_any_other_failure_is_one_short_line_under_the_windows(panel, code):
    windows = {"win-a": {"percent": 50.0}, "win-b": {"percent": 20.0}}
    name = uniq("p")
    p = panel({name: reading(name, windows)},
              run_action=scripted("", code, "boom: it broke\n" * 5))
    row = p.settle(name, lambda r: len(r["lines"]) > 2)
    assert len(row["lines"]) == 3, row["lines"]
    assert len(window_lines(row, windows)) == 2
    diagnostic = [l for l in row["lines"] if "win-" not in l]
    assert len(diagnostic) == 1 and 0 < len(diagnostic[0]) <= 120, diagnostic
    assert row["lines"].index(diagnostic[0]) == 2, "under the windows"
    time.sleep(0.3)
    assert len(p.view(with_scripts=True)[name]["lines"]) == 3, "still one, not growing"


# -- the shipped usage actions print extras only ---------------------------

BAR = re.compile(rf"[{FILLED}{EMPTY}]|#{{3,}}|\.{{5,}}")


def run_usage(script: str, budget: dict, tmp_path, **extra_env):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "TZ": "UTC", "MULTIAGENTS_BUDGET": json.dumps(budget, sort_keys=True),
           **extra_env}
    return subprocess.run(["sh", str(PROVIDERS_DIR / script), "usage"], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=60)


def assert_extras_only(cp, windows):
    assert cp.returncode in (0, 64), (cp.returncode, cp.stdout, cp.stderr)
    out = cp.stdout
    assert not BAR.search(out), f"a usage action printed a bar:\n{out}"
    assert "tightest window" not in out and "% used" not in out, out
    assert not re.search(r"\d\s*%", out), f"a window percentage was printed:\n{out}"
    for window in windows:
        assert window not in out, f"window {window!r} printed by the script:\n{out}"


def with_labels(windows):
    return {n: {**w, "resets_label": "Oct 05 16:00 CEST · in 1d21h"}
            for n, w in windows.items()}


def test_mq_r2a_claude_sh_usage_prints_the_credits_and_no_bars(tmp_path):
    windows = SHAPES["claude"]
    budget = {"known": True, "headroom": 0.05, "used_percent": 95.0,
              "resets_at": windows["session"]["resets_at"],
              "resets_label": "Oct 05 16:00 CEST · in 1d21h",
              "windows": with_labels(windows),
              "spent": {"extra_credits_used": 0, "extra_credits_limit": 8500}}
    cp = run_usage("claude.sh", budget, tmp_path)
    assert_extras_only(cp, windows)
    assert cp.returncode == 0 and "credits 0.00 of 85.00" in cp.stdout, cp.stdout


def test_mq_r2a_claude_sh_usage_with_nothing_extra_is_quiet(tmp_path):
    windows = SHAPES["claude"]
    budget = {"known": True, "headroom": 0.05, "used_percent": 95.0,
              "windows": with_labels(windows)}
    cp = run_usage("claude.sh", budget, tmp_path)
    assert_extras_only(cp, windows)


def test_mq_r2a_agy_sh_usage_prints_no_bars(tmp_path):
    windows = SHAPES["agy"]
    budget = {"known": True, "headroom": 0.8, "windows": with_labels(
        {n: {**w, "models": "Some, Models",
             "headroom": 1 - w["percent"] / 100} for n, w in windows.items()})}
    assert_extras_only(run_usage("agy.sh", budget, tmp_path), windows)


def test_mq_r2a_opencode_sh_usage_prints_no_bars(tmp_path):
    windows = SHAPES["opencode"]
    budget = {"known": True, "headroom": 0.14, "windows": with_labels(windows),
              "spent": {"total": 12000}}
    assert_extras_only(run_usage("opencode.sh", budget, tmp_path), windows)


def zai_usage(env, server):
    return env.run("usage", origin=server.origin)


def test_mq_r2a_opencode_zai_usage_keeps_its_credits_and_prints_no_bars(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 5, FIVE_MS, usage=10000, current=9963),
                     limit(6, 1, 80, WEEK_MS, usage=10000, current=8000)])
    cp = zai_usage(env, zai((200, body, {})))
    assert_extras_only(cp, ["five_hour", "weekly"])
    assert cp.returncode == 0
    assert "9963/10000 credits" in cp.stdout, cp.stdout


def test_mq_r2a_opencode_zai_usage_without_credit_fields_prints_no_bars(env, zai):
    env.zai_store()
    body = envelope([limit(3, 5, 42, FIVE_MS), limit(6, 1, 7, WEEK_MS)])
    cp = zai_usage(env, zai((200, body, {})))
    assert_extras_only(cp, ["five_hour", "weekly"])
    assert "None" not in cp.stdout and "Traceback" not in cp.stderr


# --------------------------------------------------------------------------
# MQ-R3a


def probing(state, calls=None):
    from multiagents.manifest import ProbeResult

    def fake(name, paths=None, context="host"):
        if calls is not None:
            calls.append((name, context))
        return ProbeResult(state=state)
    return fake


def row_text(row) -> str:
    return json.dumps(row, default=str, ensure_ascii=False).lower()


def shows_not_installed(row) -> bool:
    """The page says "not installed" for `available: false`; a state label may
    say it in words. Either way the row is calling the provider absent."""
    return row.get("available") is False or "not installed" in row_text(row)


def not_installed_alerts(p, name):
    rows = p.view()
    found = snap.alerts(p.paths, p.config, p.tree, p.rows)
    return [a for a in found if name in a["text"] and "not installed" in a["text"]]


@pytest.mark.parametrize("state", ["verified", "unverified", "overridden"])
def test_mq_r3a_docker_a_binary_in_the_container_is_installed_though_absent_on_host(
        panel, state):
    name, calls = uniq("agy"), []
    p = panel({name: reading(name, SHAPES["agy"])}, kind="docker", host_has=False,
              probe=probing(state, calls))
    row = p.view()[name]
    assert not shows_not_installed(row), row_text(row)
    assert not not_installed_alerts(p, name)
    assert (name, "docker") in calls, "the C5 probe, in the docker context"


def test_mq_r3a_docker_absent_in_the_container_is_not_installed_even_if_on_the_host(
        panel):
    name = uniq("agy")
    p = panel({name: reading(name, SHAPES["agy"])}, kind="docker", host_has=True,
              probe=probing("missing"))
    row = p.view()[name]
    assert shows_not_installed(row), row_text(row)
    assert len(not_installed_alerts(p, name)) == 1


@pytest.mark.parametrize("state, label", [
    ("container not running", "container not running"),
    ("probe_failed", "probe failed"),
    ("timeout", "probe failed"),
    ("no manifest", "unverified (no manifest)"),
])
def test_mq_r3a_docker_only_a_real_absence_is_called_not_installed(panel, state, label):
    name = uniq("agy")
    p = panel({name: reading(name, SHAPES["agy"])}, kind="docker", host_has=False,
              probe=probing(state))
    row = p.view()[name]
    text = row_text(row)
    assert label in text, (label, text)
    assert not shows_not_installed(row), text
    assert not not_installed_alerts(p, name), "no false 'not installed' alert"


def test_mq_r3a_docker_states_are_told_apart(panel):
    seen = {}
    for state in ("verified", "missing", "container not running", "probe_failed",
                  "no manifest"):
        name = uniq("agy")
        p = panel({name: reading(name, SHAPES["agy"])}, kind="docker",
                  host_has=False, probe=probing(state))
        row = p.view()[name]
        seen[state] = {k: v for k, v in row.items()
                       if k not in ("name", "lines", "budget", "agents")}
    assert len({json.dumps(v, default=str, sort_keys=True)
                for v in seen.values()}) == 5, "five states, five distinct readings"


def test_mq_r3a_local_executor_keeps_the_host_check(panel):
    gone, here = uniq("gone"), uniq("here")
    p = panel({gone: reading(gone, SHAPES["codex"]),
               here: reading(here, SHAPES["codex"])}, kind="local",
              host_has={gone: False, here: True},
              probe=probing("verified"))
    rows = p.view()
    assert rows[gone]["available"] is False
    assert len(not_installed_alerts(p, gone)) == 1
    assert rows[here]["available"]
    assert not not_installed_alerts(p, here)


def test_mq_r3a_local_executor_ignores_the_docker_probe(panel):
    here = uniq("here")
    p = panel({here: reading(here, SHAPES["codex"])}, kind="local", host_has=True,
              probe=probing("missing"))
    row = p.view()[here]
    assert row["available"] and not shows_not_installed(row)


# --------------------------------------------------------------------------
# MQ-R4a


@pytest.fixture
def stall():
    """A usage action that sleeps past its timeout, released at teardown."""
    release = threading.Event()
    started = []

    def action(name, provider, executor, act, *a, **k):
        started.append(name)
        release.wait(8)
        return 1, "", "timed out"
    yield action, started
    release.set()


def test_mq_r4a_a_stalled_usage_action_does_not_delay_the_window_lines(panel, stall):
    action, started = stall
    names = [uniq("slow") for _ in range(3)]
    p = panel({n: reading(n, SHAPES["opencode"]) for n in names}, run_action=action)
    t0 = time.monotonic()
    rows = p.view(with_scripts=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"a cold refresh waited {elapsed:.1f}s behind usage actions"
    for n in names:
        assert len(window_lines(rows[n], SHAPES["opencode"])) == 3, rows[n]["lines"]


def test_mq_r4a_the_next_refreshes_stay_fast_while_the_action_still_stalls(panel, stall):
    action, _ = stall
    name = uniq("slow")
    p = panel({name: reading(name, SHAPES["claude"])}, run_action=action)
    for _ in range(3):
        t0 = time.monotonic()
        row = p.view(with_scripts=True)[name]
        assert time.monotonic() - t0 < 2.0
        assert len(window_lines(row, SHAPES["claude"])) == 2
        time.sleep(0.1)


def test_mq_r4a_extras_arrive_within_a_few_refreshes_when_the_action_is_quick(panel):
    name = uniq("quick")
    p = panel({name: reading(name, SHAPES["claude"])},
              run_action=scripted("credits 1.00 of 85.00 \u2014 available\n"))
    row = p.settle(name, lambda r: any("credits" in l for l in r["lines"]))
    assert any("credits 1.00 of 85.00" in l for l in row["lines"]), row["lines"]
    assert len(window_lines(row, SHAPES["claude"])) == 2


def test_mq_r4a_a_failing_usage_action_keeps_every_window_line(panel):
    windows = SHAPES["agy"]
    name = uniq("agy")
    p = panel({name: reading(name, windows)}, kind="docker",
              probe=probing("verified"),
              run_action=scripted("", 1, "binary 'agy' not found"))
    row = p.settle(name, lambda r: len(r["lines"]) > len(windows))
    assert len(window_lines(row, windows)) == len(windows)
    assert all(FILLED in l or EMPTY in l for l in window_lines(row, windows))
    extra = [l for l in row["lines"] if l not in window_lines(row, windows)]
    assert len(extra) == 1 and not BAR.search(extra[0]), (
        "one diagnostic line, and no stray overall bar", row["lines"])


# --------------------------------------------------------------------------
# MQ-R5: the shared reading and budget_status do not move


def test_mq_r5_the_reading_the_panel_and_budget_status_share_is_unchanged():
    b = reading("acme", {
        "session": {"percent": 95.0},
        "weekly_all": {"percent": 30.0, "counted": True},
        "3p-5h": {"percent": 100.0, "counted": False}},
        headroom=0.05, resets_at=None, spent={"tokens": 5})
    assert b.to_dict() == {
        "provider": "acme", "known": True, "severity": "normal", "source": "",
        "usable": True, "headroom": 0.05, "used_percent": 95.0,
        "windows": {"session": {"percent": 95.0},
                    "weekly_all": {"percent": 30.0, "counted": True},
                    "3p-5h": {"percent": 100.0, "counted": False}},
        "spent": {"tokens": 5}}


def test_mq_r5_budget_status_is_unchanged_for_a_fixture(tmp_path, monkeypatch):
    from multiagents import server
    from test_codex_engine_models_budget import Project, _executable, NAME
    import c3_harness as h3

    p = Project(tmp_path, monkeypatch, {NAME: {"bin": NAME, "spawn": {"args": ["x"]}}})
    payload = {"known": True, "headroom": 0.2, "source": "script",
               "windows": {"alpha": {"percent": 80.0},
                           "beta": {"percent": 10.0, "counted": False}}}
    _executable(p.scripts / f"{NAME}.sh",
                "#!/bin/sh\ncase \"$1\" in budget) cat <<'EOF'\n"
                + json.dumps(payload) + "\nEOF\n;; *) exit 64;; esac\n")
    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    if isinstance(status, str):
        status = json.loads(status)
    entry = status["providers"][NAME]
    assert entry["headroom"] == 0.2 and entry["used_percent"] == 80.0
    assert entry["windows"] == payload["windows"]
    assert entry["known"] is True and entry["source"] == "script"
    assert "lines" not in entry and "lines_from" not in entry, (
        "the panel's rendering never leaks into the MCP reading")
