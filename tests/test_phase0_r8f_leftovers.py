"""Phase 0, contract B — P0-R8f.18 to P0-R8f.20, the R8f leftovers.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8f, "Added
2026-09-24, R8f leftovers (BRIEF item 4)", read with R8f.12, R8f.13 and R8f.16,
which these extend.

- **R8f.18** — `claude.sh launch` finds the session where the CLI keeps it.
  The shipped script is run as the driver runs it (`sh claude.sh launch`),
  through the scratch of `test_phase0_provider_compact.py`, with
  `MULTIAGENTS_BIN=/bin/echo` so the argv it would hand the CLI is printed.
  `HOME` and `CLAUDE_CONFIG_DIR` point at different fixture directories and the
  session file sits in only one of them; which one the script found is read off
  the argv (`--resume` / `--session-id`, `--continue` / none). Plus the
  grep-style check the contract asks for.
- **R8f.19** — a user's own exit wins over a usage-limit stop. The R8f harness
  of `test_phase0_interactive_compact.py` (a real child CLI under
  `_run_supervised`), with the fake of `test_phase0_r8f_review.py` that exits
  by itself when told. See `_ExitOnDetection` for how the exit is placed at the
  moment the limit is detected.
- **R8f.20** — the remaining driver limits parse safely. Each malformed value
  is checked by the shipped default's observable effect: how long the provider
  is paused for (`limit_wait_seconds`, `spend_limit_pause_hours`, read off the
  tree's `paused` event), whether a fast crash is retried
  (`restart_min_runtime_seconds`), and how many headless turns run
  (`supervised_turns`).

  0 per key, from `defaults/project.yaml`: none of the four documents a meaning
  for 0 (`supervised_turns` is not in the file at all), so by the contract 0 is
  malformed for each and falls back to the default. Those cases sit in each
  key's malformed parametrization as `zero`.
"""

from __future__ import annotations

import math
import re
import sys
import time as _real_time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
import test_phase0_interactive_compact as ic  # noqa: E402
import test_phase0_r8f_review as rv  # noqa: E402
from test_phase0_provider_compact import SID as LAUNCH_SID, Scratch  # noqa: E402
from test_phase0_r8f_review import ctrl_c_after  # noqa: E402,F401

from multiagents import driver, watchdog  # noqa: E402

SHIPPED_LIMITS = yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]
CLAUDE_SH = ch.PROVIDER_SCRIPTS / "claude.sh"

# R8f.20's list: not a number, negative, inf, NaN, a string, a list — and 0,
# which none of the four keys gives a meaning to.
MALFORMED = [("not_a_number", "soon"), ("negative", -1), ("inf", float("inf")),
             ("nan", float("nan")), ("inf_string", "inf"), ("list", [5]),
             ("zero", 0)]


def _malformed(*, skip: tuple = ()):
    cases = [(i, v) for i, v in MALFORMED if i not in skip]
    return pytest.mark.parametrize("value", [v for _, v in cases],
                                   ids=[i for i, _ in cases])


# ============================================================= P0-R8f.18 ==
# `claude.sh launch` reads sessions from CLAUDE_CONFIG_DIR when it is set.

def _launch_argv(s: Scratch, **env) -> tuple[list[str], str]:
    got = s.run("claude", "launch", MULTIAGENTS_BIN="/bin/echo", **env)
    assert got.returncode == 0, got.stderr
    return got.stdout.split(), got.stderr


def _session_under(root: Path, s: Scratch, name: str = f"{LAUNCH_SID}.jsonl") -> Path:
    return ch.write_transcript(root / "projects" / ch.slug(s.cwd) / name,
                               [ch.user("hi"), ch.request(27_729)])


def test_p0_r8f_18_launch_resumes_the_session_kept_under_claude_config_dir(tmp_path):
    """The contract's verification: the session exists only under
    CLAUDE_CONFIG_DIR, HOME points elsewhere, and a resume finds it."""
    s = Scratch(tmp_path)
    config = tmp_path / "claude-config"
    _session_under(config, s)
    assert not s.transcript.exists(), "harness: HOME must not hold the session"
    argv, _ = _launch_argv(s, CLAUDE_CONFIG_DIR=str(config), MULTIAGENTS_RESUME="1")
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == LAUNCH_SID, (
        f"the session under CLAUDE_CONFIG_DIR was not found; launch argv: {argv}")
    assert "--session-id" not in argv, argv


def test_p0_r8f_18_launch_does_not_resume_a_session_only_under_home(tmp_path):
    """With CLAUDE_CONFIG_DIR set, a session file under $HOME/.claude is not
    where the CLI will look, so it is not resumed: the id is created afresh
    (`--session-id`) rather than `--resume`d into a session the CLI cannot
    find."""
    s = Scratch(tmp_path)
    s.session()                                   # under $HOME/.claude only
    config = tmp_path / "claude-config"
    (config / "projects").mkdir(parents=True)
    argv, _ = _launch_argv(s, CLAUDE_CONFIG_DIR=str(config), MULTIAGENTS_RESUME="1")
    assert "--resume" not in argv, (
        f"launch resumed a session found under $HOME/.claude while "
        f"CLAUDE_CONFIG_DIR is set: {argv}")
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == LAUNCH_SID


@pytest.mark.parametrize("config_dir", [None, ""], ids=["unset", "empty"])
def test_p0_r8f_18_launch_without_claude_config_dir_reads_home(tmp_path, config_dir):
    """The other half of the rule: unset (or empty, as `default_root()` reads
    it) means $HOME/.claude."""
    s = Scratch(tmp_path)
    s.session()
    env = {} if config_dir is None else {"CLAUDE_CONFIG_DIR": config_dir}
    argv, _ = _launch_argv(s, MULTIAGENTS_RESUME="1", **env)
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == LAUNCH_SID, argv


def test_p0_r8f_18_launch_without_a_session_id_continues_from_claude_config_dir(tmp_path):
    """The id-less resume (an install predating session ids) looks for any
    conversation in the directory — under CLAUDE_CONFIG_DIR too."""
    s = Scratch(tmp_path)
    config = tmp_path / "claude-config"
    _session_under(config, s, name="older.jsonl")
    got = s.run("claude", "launch", MULTIAGENTS_BIN="/bin/echo", sid=None,
                CLAUDE_CONFIG_DIR=str(config), MULTIAGENTS_RESUME="1")
    assert got.returncode == 0, got.stderr
    argv = got.stdout.split()
    assert "--continue" in argv, (
        f"a conversation under CLAUDE_CONFIG_DIR was not continued: {argv}")


def test_p0_r8f_18_launch_without_a_session_id_ignores_home_when_config_dir_is_set(
        tmp_path):
    s = Scratch(tmp_path)
    _session_under(s.home / ".claude", s, name="older.jsonl")
    config = tmp_path / "claude-config"
    (config / "projects").mkdir(parents=True)
    got = s.run("claude", "launch", MULTIAGENTS_BIN="/bin/echo", sid=None,
                CLAUDE_CONFIG_DIR=str(config), MULTIAGENTS_RESUME="1")
    assert got.returncode == 0, got.stderr
    assert "--continue" not in got.stdout.split(), (
        f"launch continued a conversation under $HOME/.claude while "
        f"CLAUDE_CONFIG_DIR is set: {got.stdout}")


_HOME_CLAUDE = re.compile(r"(\$\{?HOME\b[^/\s]*/\.claude|~/\.claude)")


def test_p0_r8f_18_no_action_hard_codes_home_claude_for_sessions():
    """The grep-style check: every place the script composes a path from
    $HOME/.claude (the fallback) does so in the same expression that prefers
    CLAUDE_CONFIG_DIR. A line that names $HOME/.claude without it is a read
    that ignores the override. Comments, and text the script only prints, are
    not reads."""
    offenders = []
    for n, line in enumerate(CLAUDE_SH.read_text().splitlines(), 1):
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        if re.match(r"(echo|printf)\b", code):
            continue
        if _HOME_CLAUDE.search(code) and "CLAUDE_CONFIG_DIR" not in code:
            offenders.append(f"{n}: {code}")
    assert offenders == [], (
        "claude.sh still reads sessions under a hard-coded $HOME/.claude:\n"
        + "\n".join(offenders))


# ============================================================= P0-R8f.19 ==
# A user's own exit wins over a usage-limit stop.

LIMIT_MARKERS = [{"match": "usage limit reached", "resets": True,
                  "detail": "usage window"}]
SPEND_MARKERS = [{"match": "spend limit reached", "resets": False,
                  "detail": "spend cap"}]
LIMIT_RECORD = ch.limit_message("Claude usage limit reached.")


def _limited(s: ic.Session, text: str = "Claude usage limit reached.") -> None:
    ch.write_transcript(s.transcript, [ch.user("go"), ch.limit_message(text)])


def _fake() -> str:
    """The R8f.12 fake (exits by itself when told, records its pid), plus: a
    launch entry with `touch_events` appends one line to the tree's event log,
    so a headless turn counts as having done something."""
    fake = rv._extended_fake()
    anchor = "    me = launches[min(n, len(launches) - 1)]\n"
    assert anchor in fake, "harness: the R8f fake has changed shape"
    return fake.replace(anchor, anchor + (
        "    if me.get(\"touch_events\"):\n"
        "        with open(ctl[\"events\"], \"a\") as fh:\n"
        "            fh.write(json.dumps({\"t\": time.time(), \"agent\": \"x\", "
        "\"kind\": \"note\"}) + \"\\n\")\n"))


@pytest.fixture
def session(tmp_path, monkeypatch, capsys):
    def build(**kwargs) -> ic.Session:
        s = ic.Session(tmp_path, monkeypatch, capsys, **kwargs)
        s.script.write_text(_fake())
        return s
    return build


class _ExitOnDetection:
    """`watchdog.limit_reached`, unchanged in what it answers, that makes the
    CLI exit by itself at the `nth` time it reports a limit.

    It touches the CLI's exit flag, waits until the CLI has actually exited,
    and only then hands the detection back to the driver. So the CLI's own
    exit lands after the limit was seen and before the driver can act on it:
    with `nth=1` while the driver is about to stop it (the detection that
    starts the one-poll warning), with `nth=2` once it has decided to (the
    detection the warning ends with). Nothing else about detection is changed.
    """

    def __init__(self, s: ic.Session, flag: Path, nth: int):
        self.s, self.flag, self.nth = s, flag, nth
        self.real = watchdog.limit_reached
        self.seen = 0
        self.fired = False

    def __call__(self, provider, cwd):
        found = self.real(provider, cwd)
        if found is not None and not self.fired:
            self.seen += 1
            if self.seen == self.nth:
                self.fired = True
                self.flag.touch()
                rv._wait_for(self._current_exited, 10)
        return found

    def _current_exited(self) -> bool:
        """The CLI running now (the latest launch) has exited, not an earlier one."""
        n = len(self.s.calls("launch"))
        mine = [e for e in self.s.calls("exit") if e["n"] == n]
        return bool(mine) and rv._dead(mine[0]["pid"])


def _no_limit_wait(s: ic.Session, out: str) -> None:
    assert s.events("paused") == [], (
        f"the driver paused for the limit after the user's own exit:\n{out}")
    assert "Waiting" not in out, f"the driver waited out a window:\n{out}"


@pytest.mark.parametrize("nth", [1, 2], ids=["about_to_stop", "decided_to_stop"])
@pytest.mark.parametrize("own_exit", [0, 1], ids=["exit_0", "exit_1"])
def test_p0_r8f_19_a_cli_that_exits_as_the_limit_is_detected_is_not_waited_for(
        session, monkeypatch, tmp_path, nth, own_exit):
    """R8f.19's verification: the CLI exits by itself at the moment the limit
    is detected. No limit wait, no relaunch, and the session ends as it would
    with no limit: 0 for a normal exit; for exit 1 the crash path, which is
    not retried by default."""
    s = session(compact_at=0, limit_markers=LIMIT_MARKERS,
                limits={"limit_wait_seconds": 0.001})
    _limited(s)
    flag = tmp_path / "exit.now"
    hook = _ExitOnDetection(s, flag, nth)
    monkeypatch.setattr(watchdog, "limit_reached", hook)
    code, out = s.run([{"life": ic.STOPPED_BY, "exit_when": str(flag),
                        "exit": own_exit}, {"life": 0}])
    if not hook.fired:
        pytest.skip("inconclusive: the driver never reported the limit "
                    f"{nth} time(s) while the CLI ran")
    assert s.calls("exit") and s.calls("exit")[0]["n"] == 1, (
        "harness: the CLI did not exit by itself")
    assert s.stops() == [], "harness: the CLI was stopped, not exited"
    _no_limit_wait(s, out)
    assert len(s.calls("launch")) == 1, (
        f"the session was relaunched after the user's own exit: {s.seq()}")
    assert code == (0 if own_exit == 0 else 1), out


def test_p0_r8f_19_the_rule_holds_on_a_relaunch_too(session, monkeypatch, tmp_path):
    """The attached CLI of a retry (after a lost terminal) is held on the same
    terms: its own exit as the limit is decided ends the session."""
    s = session(compact_at=0, limit_markers=LIMIT_MARKERS,
                limits={"limit_wait_seconds": 0.001})
    ch.write_transcript(s.transcript, [ch.user("go")])
    flag = tmp_path / "exit.now"
    hook = _ExitOnDetection(s, flag, 2)
    monkeypatch.setattr(watchdog, "limit_reached", hook)
    code, out = s.run([{"exit": 129},
                       {"life": ic.STOPPED_BY, "exit_when": str(flag),
                        "appends": [[0, [LIMIT_RECORD]]]},
                       {"life": 0}])
    if not hook.fired:
        pytest.skip("inconclusive: the limit was not reported twice during the "
                    "relaunch")
    assert s.stops() == [], "harness: the CLI was stopped, not exited"
    _no_limit_wait(s, out)
    assert len(s.calls("launch")) == 2, (
        f"relaunched after the user's own exit on the retry: {s.seq()}")
    assert code == 0, out


def test_p0_r8f_19_a_cli_the_driver_stopped_for_the_limit_is_waited_for_and_resumed(
        session):
    """The other half: a CLI still running when the limit is acted on is
    stopped, the window is waited out, and the session is relaunched."""
    s = session(compact_at=0, limit_markers=LIMIT_MARKERS,
                limits={"limit_wait_seconds": 0.001})
    _limited(s)
    code, out = s.run([{"life": ic.STOPPED_BY}, {"life": 0}])
    assert len(s.stops()) == 1, f"the limited CLI was not stopped: {s.seq()}"
    assert len(s.events("paused")) >= 1, f"no limit wait:\n{out}"
    assert len(s.calls("launch")) == 2, f"not relaunched after the wait: {s.seq()}"
    assert s.calls("launch")[1]["env"].get("MULTIAGENTS_RESUME") == "1"
    assert code == 0, out


# ============================================================= P0-R8f.20 ==
# limit_wait_seconds, restart_min_runtime_seconds, supervised_turns and
# spend_limit_pause_hours: malformed -> the shipped default, never a crash.

def _run(s: ic.Session, launches: list[dict]) -> tuple[int, str]:
    """`s.run`, with a crash reported as the contract failure it is."""
    try:
        return s.run(launches)
    except Exception as exc:                                  # noqa: BLE001
        pytest.fail(f"the driver crashed on a malformed limit: {exc!r}")


def _paused_for(s: ic.Session) -> float:
    """How long the first pause of the run was for, in seconds."""
    paused = s.events("paused")
    assert paused, "harness: the provider was never paused"
    until = paused[0].get("until")
    assert isinstance(until, (int, float)) and math.isfinite(until), (
        f"the pause has no finite end: {until!r}")
    return until - paused[0]["t"]


def test_p0_r8f_20_the_defaults_this_section_relies_on_are_shipped():
    assert SHIPPED_LIMITS["limit_wait_seconds"] == 900
    assert SHIPPED_LIMITS["restart_min_runtime_seconds"] == 60
    assert SHIPPED_LIMITS["spend_limit_pause_hours"] == 12


def test_p0_r8f_20_supervised_turns_has_a_shipped_default():
    """"Falls back to the shipped default" needs one: `supervised_turns` is
    read by the driver but is not in `defaults/project.yaml`, so a malformed
    value has nothing shipped to fall back to."""
    value = SHIPPED_LIMITS.get("supervised_turns")
    assert isinstance(value, int) and not isinstance(value, bool) and value > 0, (
        f"defaults/project.yaml ships no positive supervised_turns: {value!r}")


# --- limit_wait_seconds -------------------------------------------------

def _wait_session(session, value):
    s = session(compact_at=0, limit_markers=LIMIT_MARKERS,
                limits={"limit_wait_seconds": value})
    _limited(s)
    return s


def test_p0_r8f_20_an_explicit_limit_wait_is_honoured(session, ctrl_c_after):
    """Control: 120 s is a 120 s pause. Ctrl-C during the wait ends the run."""
    s = _wait_session(session, 120)
    ctrl_c_after(3.0)
    code, out = _run(s, [{"life": ic.STOPPED_BY}])
    assert abs(_paused_for(s) - 120) < 5, out
    assert len(s.calls("launch")) == 1 and code == 0, out


@_malformed()
def test_p0_r8f_20_a_malformed_limit_wait_is_the_default_900s(
        session, ctrl_c_after, value):
    """A limit that resets is waited out for the default 900 s (the first
    wait): the provider is paused that long, and nothing is relaunched while
    the wait runs. Ctrl-C during the wait ends the run with 0."""
    s = _wait_session(session, value)
    ctrl_c_after(3.0)
    code, out = _run(s, [{"life": ic.STOPPED_BY}, {"life": 0}])
    assert abs(_paused_for(s) - 900) < 5, (
        f"a malformed limit_wait_seconds did not wait the default 900s:\n{out}")
    assert len(s.calls("launch")) == 1, (
        f"relaunched within 3s: the default wait was not kept ({s.seq()})")
    assert code == 0, out


# --- spend_limit_pause_hours --------------------------------------------

def _spend_session(session, value):
    s = session(compact_at=0, limit_markers=SPEND_MARKERS,
                limits={"spend_limit_pause_hours": value})
    _limited(s, "Claude spend limit reached.")
    return s


def test_p0_r8f_20_an_explicit_spend_pause_is_honoured(session):
    """Control: a limit that does not reset holds the provider for 2 h and
    stops the run with 3."""
    s = _spend_session(session, 2)
    code, out = _run(s, [{"life": ic.STOPPED_BY}])
    assert abs(_paused_for(s) - 2 * 3600) < 60, out
    assert code == 3, out


@_malformed()
def test_p0_r8f_20_a_malformed_spend_pause_is_the_default_12h(session, value):
    s = _spend_session(session, value)
    code, out = _run(s, [{"life": ic.STOPPED_BY}])
    assert abs(_paused_for(s) - 12 * 3600) < 60, (
        f"a malformed spend_limit_pause_hours did not hold the default 12h:\n{out}")
    assert code == 3, out
    assert len(s.calls("launch")) == 1


# --- restart_min_runtime_seconds ----------------------------------------

def _crash_session(session, value):
    return session(compact_at=0, limits={"restart_on_crash": True,
                                         "restart_min_runtime_seconds": value})


def test_p0_r8f_20_an_explicit_min_runtime_is_honoured(session):
    """Control: with a 0.5 s minimum, a crash after 1.5 s is retried."""
    s = _crash_session(session, 0.5)
    code, out = _run(s, [{"life": 1.5, "exit": 1}, {"exit": 0}])
    assert len(s.calls("launch")) == 2, (s.seq(), out)
    assert code == 0, out


@_malformed()
def test_p0_r8f_20_a_malformed_min_runtime_is_the_default_60s(session, value):
    """With restart_on_crash on, a crash well inside the default 60 s is the
    same fault read again: not retried, the run ends with 1. (`inf` agrees with
    the default here; the test below is the one that tells them apart.)"""
    s = _crash_session(session, value)
    code, out = _run(s, [{"exit": 1}, {"exit": 0}])
    assert len(s.calls("launch")) == 1, (
        f"a crash inside the default 60s was retried with a malformed "
        f"restart_min_runtime_seconds ({s.seq()}):\n{out}")
    assert code == 1, out


class _LaterTime:
    """The `time` module as the driver sees it, with a monotonic clock that a
    test moves on. Everything else is the real module."""

    def __init__(self):
        self.skew = 0.0

    def monotonic(self) -> float:
        return _real_time.monotonic() + self.skew

    def __getattr__(self, name):
        return getattr(_real_time, name)


def test_p0_r8f_20_an_inf_min_runtime_still_retries_a_crash_past_60s(session, monkeypatch):
    """The only way to tell `inf` from the default is a crash after more than
    60 s, which the default retries and `inf` never would.

    TS-R2: the child really lives 1.5 s, and the driver's monotonic clock is
    moved on 60 s as each attached run returns, so the driver measures about
    61.5 s for it. The 60 s compared against is still the shipped default."""
    clock = _LaterTime()
    monkeypatch.setattr(driver, "time", clock)
    attached = driver._run_attached

    def ran_a_minute_longer(*args, **kwargs):
        code = attached(*args, **kwargs)
        clock.skew += 60
        return code

    monkeypatch.setattr(driver, "_run_attached", ran_a_minute_longer)
    s = _crash_session(session, float("inf"))
    code, out = _run(s, [{"life": 1.5, "exit": 1}, {"exit": 0}])
    assert len(s.calls("launch")) == 2, (
        f"a crash after 61s was not retried with restart_min_runtime_seconds "
        f"= inf ({s.seq()}):\n{out}")
    assert code == 0, out


# --- supervised_turns ---------------------------------------------------

class _NoTty(ic._Tty):
    """stdin once the terminal is gone."""

    def isatty(self) -> bool:
        return False


# The first launch loses its terminal (129); every headless turn after it
# writes an event, so none is idle and the loop runs to its turn limit.
LOST_THEN_BUSY = [{"exit": 129}, {"touch_events": True}]


def _headless_turns(session, monkeypatch, value=None, *,
                    omit: bool = False) -> tuple[int, int, str]:
    if omit:
        s = session(compact_at=0, omit=("supervised_turns",))
    else:
        s = session(compact_at=0, limits={"supervised_turns": value})
    s.reading(1000)                         # a typed user turn: work to continue
    monkeypatch.setattr(sys, "stdin", _NoTty())
    code, out = _run(s, LOST_THEN_BUSY)
    turns = [c for c in s.calls("launch")
             if c["env"].get("MULTIAGENTS_UNATTENDED") == "1"]
    return code, len(turns), out


def test_p0_r8f_20_an_explicit_supervised_turns_is_honoured(session, monkeypatch):
    """Control: three busy turns, then the turn limit ends the run with 0."""
    code, turns, out = _headless_turns(session, monkeypatch, 3)
    assert turns == 3, out
    assert code == 0, out


def test_p0_r8f_20_an_omitted_supervised_turns_is_the_default(session, monkeypatch):
    """A project that does not set the key gets the shipped default (50, what
    such a project gets today, while the key is unshipped)."""
    code, turns, out = _headless_turns(session, monkeypatch, omit=True)
    assert turns == SHIPPED_LIMITS.get("supervised_turns", 50), out[-2000:]
    assert code == 0, out[-2000:]


@_malformed()
def test_p0_r8f_20_a_malformed_supervised_turns_is_the_default(
        session, monkeypatch, value):
    """The headless loop runs the shipped default number of busy turns — not
    zero, not unbounded, and without crashing. (While the key is unshipped
    the default a project gets today, 50, is what is expected.)"""
    code, turns, out = _headless_turns(session, monkeypatch, value)
    expected = SHIPPED_LIMITS.get("supervised_turns", 50)
    assert turns == expected, (
        f"a malformed supervised_turns ran {turns} headless turns, not the "
        f"default {expected}:\n{out[-2000:]}")
    assert code == 0, out[-2000:]
