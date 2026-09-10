"""The terminal front end. Same snapshot, same actions, no browser.

`curses` rather than a TUI framework: the project ships with two dependencies
and a monitor is not a good reason for a third, particularly one whose whole
job is drawing. What curses costs is layout convenience; what it buys is that
`multiagents monitor --tui` works over ssh on a machine where nothing has been
installed but this.

The keys are the point. A monitor you have to reach for a mouse to use is one
you will not open while something is going wrong, so every action has a letter
and the destructive ones ask first, in the same words the web page asks in.
"""

from __future__ import annotations

import curses
import time
from typing import Any

from ..config import load as load_config
from . import actions, settings as settings_mod, snapshot as snap

REFRESH = 2.0
TABS = ("live", "config", "history", "costs")

HELP = "tab/1-4 pages · ↑↓ select · enter open · s steer · x stop · m merge · " \
       "d discard · a answer · r refresh · q quit"


class Screen:
    """One curses session. All the state a redraw needs lives here."""

    def __init__(self, stdscr, paths):
        self.stdscr = stdscr
        self.paths = paths
        self.tab = "live"
        self.cursor = 0
        self.rows: list[dict] = []            # what the cursor indexes into
        self.state: dict = {}
        self.settings: list[dict] = []
        self.message = ""
        self.transcript: dict | None = None
        self.open: set[str] = set()
        self.last_poll = 0.0
        self.scroll = 0

    # -- data --------------------------------------------------------

    def poll(self, force: bool = False) -> None:
        if not force and time.time() - self.last_poll < REFRESH:
            return
        config = load_config(self.paths)
        try:
            self.state = snap.snapshot(self.paths, config)
        except Exception as exc:
            self.message = f"snapshot failed: {type(exc).__name__}: {exc}"
        if self.tab == "config" and not self.settings:
            try:
                self.settings = settings_mod.describe(self.paths, config)
            except Exception as exc:
                self.message = f"settings failed: {exc}"
        self.last_poll = time.time()

    def do(self, name: str, **payload) -> None:
        out = actions.perform(self.paths, name, payload)
        self.message = out.get("message", "")
        self.last_poll = 0.0

    # -- drawing helpers ---------------------------------------------

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.stdscr.getmaxyx()
        if 0 <= y < height and x < width:
            try:
                self.stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
            except curses.error:              # the bottom-right cell always throws
                pass

    def colour(self, status: str) -> int:
        return {
            "running": curses.color_pair(1), "idle": curses.color_pair(3),
            "done": curses.color_pair(0), "merged": curses.color_pair(0),
            "failed": curses.color_pair(2), "orphaned": curses.color_pair(2),
            "cancelled": curses.color_pair(2), "discarded": curses.color_pair(2),
        }.get(status, 0)

    # -- pages -------------------------------------------------------

    def draw_live(self, top: int) -> int:
        y = top
        alerts = self.state.get("alerts") or []
        for alert in alerts[:4]:
            pair = curses.color_pair(2) if alert["level"] == "error" else curses.color_pair(3)
            self.put(y, 2, f"! {alert['text']}", pair | curses.A_BOLD)
            if alert.get("detail"):
                self.put(y, 4 + len(alert["text"]), f"  {alert['detail'][:60]}",
                         curses.color_pair(4))
            y += 1
        if alerts:
            y += 1

        running = self.state.get("running") or []
        self.put(y, 0, f" RUNNING ({len(running)})", curses.A_BOLD)
        y += 1
        self.rows = []
        if not running:
            self.put(y, 2, "nothing running", curses.color_pair(4))
            y += 1
        for index, agent in enumerate(running):
            self.rows.append({"kind": "agent", "id": agent["id"], "agent": agent})
            mark = ">" if index == self.cursor else " "
            self.put(y, 0, f"{mark} {agent['agent']:<14} {agent['id']:<10}",
                     self.colour(agent["status"])
                     | (curses.A_REVERSE if index == self.cursor else 0))
            self.put(y, 28, f"{agent['provider']}/{agent['model']}"[:26],
                     curses.color_pair(4))
            self.put(y, 56, f"{_num(agent['tokens']):>7} tok  {_age(agent['elapsed']):>5}"
                            f"  {agent['steps']:>3} steps  {agent['status']}")
            y += 1

        # Apart from the running list, and described as what they are: parked
        # conversations with no process, which the orchestrator resumes by
        # session id. Listing them as "running" is what made somebody ask
        # whether it was safe to stop them.
        parked = self.state.get("conversations") or []
        if parked:
            y += 1
            self.put(y, 0, f" STANDING CONVERSATIONS ({len(parked)})"
                           f"  parked, resumable, costing nothing", curses.A_BOLD)
            y += 1
            offset = len(self.rows)
            for index, agent in enumerate(parked):
                self.rows.append({"kind": "agent", "id": agent["id"], "agent": agent})
                mark = ">" if offset + index == self.cursor else " "
                quiet = (self.state.get("at", 0) - (agent.get("last_spoke") or 0))
                self.put(y, 0, f"{mark} {agent['agent']:<14} {agent['id']:<10}",
                         curses.A_REVERSE if offset + index == self.cursor else 0)
                self.put(y, 28, f"{agent['provider']}/{agent['model']}"[:26],
                         curses.color_pair(4))
                self.put(y, 56, f"{_num(agent['tokens']):>7} tok of context   "
                                f"last consulted {_age(quiet)} ago",
                         curses.color_pair(4))
                y += 1

        y += 1
        self.put(y, 0, " PROVIDERS", curses.A_BOLD)
        y += 1
        for provider in self.state.get("providers") or []:
            budget = provider.get("budget") or {}
            used = budget.get("used_percent")
            bar = ""
            if used is not None:
                filled = int(round(used / 10))
                bar = "█" * filled + "░" * (10 - filled) + f" {used:>3.0f}%"
            self.put(y, 2, f"{provider['name']:<10} {bar}",
                     curses.color_pair(2) if budget.get("severity") == "critical"
                     else curses.color_pair(3) if budget.get("severity") == "warning"
                     else 0)
            y += 1
            for line in (provider.get("lines") or [])[:4]:
                self.put(y, 14, line[:80], curses.color_pair(4))
                y += 1

        questions = [q for q in self.state.get("questions") or [] if not q.get("answered_at")]
        if questions:
            y += 1
            self.put(y, 0, f" WAITING ON YOU ({len(questions)})",
                     curses.A_BOLD | curses.color_pair(3))
            y += 1
            for question in questions[:3]:
                self.rows.append({"kind": "question", "id": question["id"],
                                  "question": question})
                self.put(y, 2, f"{question.get('agent', '?'):<14} "
                               f"{question.get('question', '')[:70]}")
                y += 1
            self.put(y, 2, "press a to answer the first one", curses.color_pair(4))
            y += 1
        return y

    def draw_history(self, top: int) -> int:
        self.rows = []
        flat: list[tuple[int, dict]] = []

        def walk(node: dict, depth: int) -> None:
            flat.append((depth, node))
            if node["id"] in self.open:
                for kid in node.get("kids") or []:
                    walk(kid, depth + 1)

        for root in self.state.get("history") or []:
            walk(root, 0)

        height, _ = self.stdscr.getmaxyx()
        window = max(4, height - top - 3)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + window:
            self.scroll = self.cursor - window + 1

        y = top
        for index, (depth, node) in enumerate(flat[self.scroll:self.scroll + window],
                                              start=self.scroll):
            self.rows.append({"kind": "agent", "id": node["id"], "agent": node})
            twist = ("▾" if node["id"] in self.open else "▸") if node.get("kids") else " "
            label = f"{'  ' * depth}{twist} {node['agent']} {node['id']}"
            self.put(y, 0, f"{label:<46}", self.colour(node["status"])
                     | (curses.A_REVERSE if index == self.cursor else 0))
            self.put(y, 48, f"{_num(node['tokens']):>7} tok  {_age(node['elapsed']):>5}"
                            f"  {node['status']:<10} {node.get('branch', '')[:28]}",
                     curses.color_pair(4))
            y += 1
        self.rows = [{"kind": "agent", "id": n["id"], "agent": n} for _, n in flat]

        if self.transcript:
            y += 1
            self.put(y, 0, f" TRANSCRIPT {self.transcript['id']}", curses.A_BOLD)
            y += 1
            for entry in (self.transcript.get("entries") or [])[-8:]:
                head = f"{entry['kind']}{' · ' + entry['tool'] if entry['tool'] else ''}"
                self.put(y, 2, head, curses.color_pair(4))
                y += 1
                for line in (entry["text"] or "").splitlines()[:3]:
                    self.put(y, 4, line[:110])
                    y += 1
        return y

    def draw_costs(self, top: int) -> int:
        totals = self.state.get("totals") or {}
        grand = totals.get("grand") or {}
        y = top
        self.put(y, 0, f" {grand.get('runs', 0)} runs · "
                       f"{_num(grand.get('tokens', 0))} tokens · "
                       f"${grand.get('cost_usd', 0):.2f}", curses.A_BOLD)
        y += 2
        for title, key in (("BY DAY", "by_day"), ("BY AGENT", "by_agent"),
                           ("BY MODEL", "by_model")):
            self.put(y, 0, f" {title}", curses.A_BOLD)
            y += 1
            rows = sorted((totals.get(key) or {}).items(),
                          key=lambda kv: -kv[1]["cost_usd"])[:8]
            for name, row in rows:
                self.put(y, 2, f"{name[:34]:<36} {row['runs']:>4} runs "
                               f"{_num(row['tokens']):>9} tok  ${row['cost_usd']:.4f}")
                y += 1
            y += 1
        self.rows = []
        return y

    def draw_config(self, top: int) -> int:
        height, _ = self.stdscr.getmaxyx()
        window = max(4, height - top - 3)
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + window:
            self.scroll = self.cursor - window + 1

        self.rows = [{"kind": "setting", "id": s["key"], "setting": s}
                     for s in self.settings]
        y = top
        for index in range(self.scroll, min(self.scroll + window, len(self.settings))):
            setting = self.settings[index]
            selected = index == self.cursor
            self.put(y, 0, f"{setting['key'][:52]:<54}",
                     curses.A_REVERSE if selected else 0)
            self.put(y, 56, f"{str(setting['value'])[:24]:<26} {setting['kind']}",
                     curses.color_pair(4))
            y += 1
        if self.settings and self.cursor < len(self.settings):
            current = self.settings[self.cursor]
            y += 1
            self.put(y, 0, " enter edits this setting", curses.color_pair(4))
            y += 1
            for line in (current.get("help") or "").splitlines()[:4]:
                self.put(y, 2, line[:110], curses.color_pair(4))
                y += 1
        return y

    # -- the loop ----------------------------------------------------

    def draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        project = self.state.get("project") or {}
        driving = ", ".join(d["role"] for d in (self.state.get("drivers") or [])
                            if d.get("running")) or "idle"
        head = (f" multiagents · {project.get('name', '?')} · "
                f"{project.get('executor', '')} · {driving}")
        self.put(0, 0, head + " " * max(0, width - len(head)), curses.A_REVERSE)
        tabs = "  ".join(f"[{i + 1}]{name}" for i, name in enumerate(TABS))
        self.put(1, 1, tabs.replace(f"[{TABS.index(self.tab) + 1}]{self.tab}",
                                    f"[{TABS.index(self.tab) + 1}]{self.tab.upper()}"))
        counts = self.state.get("counts") or {}
        self.put(1, max(0, width - 44),
                 f"{counts.get('running', 0)} running · "
                 f"{counts.get('conversations', 0)} parked · "
                 f"{counts.get('open_questions', 0)}q · {counts.get('open_tickets', 0)}t")

        drawer = {"live": self.draw_live, "history": self.draw_history,
                  "costs": self.draw_costs, "config": self.draw_config}[self.tab]
        try:
            drawer(3)
        except Exception as exc:              # never leave a blank screen
            self.put(4, 2, f"draw failed: {type(exc).__name__}: {exc}",
                     curses.color_pair(2))

        if self.message:
            self.put(height - 2, 0, f" {self.message[:width - 2]}",
                     curses.color_pair(3) | curses.A_BOLD)
        self.put(height - 1, 0, HELP[:width - 1], curses.A_REVERSE)
        self.stdscr.refresh()

    def prompt(self, question: str) -> str:
        """Ask for a line of text. Echo on, cursor visible, then back to normal."""
        height, width = self.stdscr.getmaxyx()
        self.put(height - 2, 0, " " * (width - 1))
        self.put(height - 2, 0, f" {question} ", curses.A_BOLD)
        curses.echo()
        curses.curs_set(1)
        self.stdscr.nodelay(False)
        try:
            raw = self.stdscr.getstr(height - 2, len(question) + 2, 200)
            return raw.decode(errors="replace").strip()
        except Exception:
            return ""
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.stdscr.nodelay(True)

    def confirm(self, question: str) -> bool:
        return self.prompt(f"{question} [y/N]").lower().startswith("y")

    def selected(self) -> dict | None:
        if 0 <= self.cursor < len(self.rows):
            return self.rows[self.cursor]
        return None

    def key(self, char: int) -> bool:
        """Handle one keystroke. False means quit."""
        row = self.selected()
        agent = (row or {}).get("agent") or {}

        if char in (ord("q"), 27):
            return False
        if char == ord("\t"):
            self.tab = TABS[(TABS.index(self.tab) + 1) % len(TABS)]
            self.cursor = self.scroll = 0
        elif char in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.tab = TABS[char - ord("1")]
            self.cursor = self.scroll = 0
        elif char in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(self.cursor + 1, max(0, len(self.rows) - 1))
        elif char in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
        elif char == ord("r"):
            self.last_poll = 0.0
            self.settings = []
        elif char in (curses.KEY_ENTER, 10, 13):
            if self.tab == "config" and row:
                self.edit(row["setting"])
            elif agent:
                self.transcript = snap.transcript(self.paths, agent["id"])
        elif char == ord(" ") and agent:
            self.open.symmetric_difference_update({agent["id"]})
        elif char == ord("s") and agent:
            message = self.prompt(f"steer {agent['id']}:")
            if message:
                self.do("steer_agent", agent_id=agent["id"], message=message)
        elif char == ord("x") and agent:
            if agent.get("parked"):
                # Different act, different word: nothing is lost but the
                # context, and the next consultation starts cold.
                question = (f"end {agent['agent']}'s conversation? "
                            f"{_num(agent['tokens'])} tokens of context go")
            else:
                question = f"stop {agent['id']}?"
            if self.confirm(question):
                self.do("stop_agent", agent_id=agent["id"])
        elif char == ord("m") and agent:
            if self.confirm(f"merge {agent.get('branch') or agent['id']}?"):
                self.do("merge_agent", agent_id=agent["id"])
        elif char == ord("d") and agent:
            if self.confirm(f"DISCARD {agent.get('branch') or agent['id']}? work is lost"):
                self.do("discard_agent", agent_id=agent["id"], force=True)
        elif char == ord("a"):
            open_questions = [q for q in self.state.get("questions") or []
                              if not q.get("answered_at")]
            if open_questions:
                answer = self.prompt(f"answer for {open_questions[0].get('agent', '?')}:")
                if answer:
                    self.do("answer_question",
                            question_id=open_questions[0]["id"], answer=answer)
        elif char == ord("X"):
            if self.confirm("stop EVERYTHING? work is kept"):
                self.do("stop_all")
        return True

    def edit(self, setting: dict) -> None:
        """Change one setting, offering its choices when it has any."""
        if setting["kind"] == "bool":
            value = not setting["value"]
        elif setting["choices"]:
            options = " / ".join(setting["choices"])
            value = self.prompt(f"{setting['key']} ({options}):")
            if not value and value not in setting["choices"]:
                return
        else:
            value = self.prompt(f"{setting['key']} = ")
            if value == "":
                return
        self.do("set_setting", file=setting["file"], path=setting["path"],
                value=value, kind=setting["kind"])
        self.settings = []


def _num(value: Any) -> str:
    value = int(value or 0)
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def _age(seconds: Any) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def _main(stdscr, paths) -> int:
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.use_default_colors()
    for index, colour in enumerate((curses.COLOR_GREEN, curses.COLOR_RED,
                                    curses.COLOR_YELLOW, curses.COLOR_CYAN),
                                   start=1):
        curses.init_pair(index, colour, -1)

    screen = Screen(stdscr, paths)
    screen.poll(force=True)
    while True:
        screen.poll()
        screen.draw()
        char = stdscr.getch()
        if char == -1:
            time.sleep(0.1)
            continue
        if not screen.key(char):
            return 0


def run(paths) -> int:
    """Draw the monitor in this terminal until q."""
    return curses.wrapper(_main, paths)
