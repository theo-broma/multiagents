"""Phase 0, contract B — P0-R8f.10 to P0-R8f.13, from the review of 9216305.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8f, "Added from the
review of 9216305 (reviewer ag-e8565d, 2026-09-23)", read with the "Decided,
from the implementer's read (ag-829577)" block above it (malformed limits fall
back to the shipped default, not to off).

- **R8f.10** — SIGTERM restores the terminal. The attached driver and the team
  picker run as real processes whose stdin is a pseudo-terminal. The test
  records the pty's attributes before the run, lets the child put it in raw
  (the attached CLI) or cbreak (the picker) mode, sends SIGTERM, and reads the
  attributes back from its own end of the pty.
- **R8f.11** — a captured action never outlives its caller. `scripts.run_action`
  runs a provider script that starts a grandchild and sleeps; an exception is
  raised into the waiting caller from a timer signal, as Ctrl-C would be. What
  is asserted is `/proc`: no process of the action's group remains.
- **R8f.12** — the user's own exit wins. Uses the R8f harness of
  `test_phase0_interactive_compact.py`, with its fake CLI extended to exit by
  itself when told. See `_ExitsWhenTold` for how the exit is placed inside the
  window between "the grace period has run out" and "the driver stops it".
- **R8f.13** — the driver's other limits parse safely. Each malformed value is
  checked by the shipped default's observable effect: the retry count and
  delay the driver prints and keeps to, the number of limit waits before it
  gives up, whether a reading is compacted, what the server reports.
"""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import termios
import time
import types
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
import test_phase0_interactive_compact as ic  # noqa: E402
from test_phase0_context_window import served  # noqa: E402,F401

from multiagents import driver, scripts, server  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402

SHIPPED_LIMITS = yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]

MALFORMED = [("not_a_number", "soon"), ("negative", -1), ("inf", float("inf")),
             ("nan", float("nan")), ("inf_string", "inf")]


def _malformed(*, strings: tuple = ()):
    cases = MALFORMED + [(f"string_{s}", s) for s in strings]
    return pytest.mark.parametrize("value", [v for _, v in cases],
                                   ids=[i for i, _ in cases])


# ============================================================= P0-R8f.10 ==
# SIGTERM restores the terminal, for the attached driver and for the picker.

def _clean_env(tmp_path: Path) -> dict:
    """The environment for a child process: nothing of an agent's own
    MULTIAGENTS_* settings except the per-test machine roots conftest set."""
    keep = {"MULTIAGENTS_STATE_DIR", "MULTIAGENTS_CONFIG_DIR"}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MULTIAGENTS_") or k in keep}
    env["PYTHONUNBUFFERED"] = "1"
    return env


class _Pty:
    """A pseudo-terminal whose slave end is the child's stdin. The test keeps
    its own descriptor on the slave, so it can read the attributes back after
    the child is gone."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)

    def attrs(self):
        return termios.tcgetattr(self.slave)

    def canonical(self) -> bool:
        return bool(self.attrs()[3] & termios.ICANON)

    def close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def _wait_for(predicate, timeout: float, proc: subprocess.Popen | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if proc is not None and proc.poll() is not None:
            return predicate()
        time.sleep(0.02)
    return predicate()


def _reap_group(proc: subprocess.Popen) -> None:
    """Teardown: nothing the test started may outlive it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


RAW_CLI = r'''
import os, sys, time, tty
tty.setraw(0)
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(120)
'''

ATTACHED_DRIVER = r'''
import os, sys
from multiagents import driver
cli = [sys.executable, "-c", sys.argv[1], sys.argv[2]]
sys.exit(driver._run_attached(cli, dict(os.environ)))
'''


def test_p0_r8f_10_sigterm_during_an_attached_run_restores_the_terminal(tmp_path):
    """The attached CLI puts the terminal in raw mode, as a TUI does, and does
    not put it back. The driver holding it is sent SIGTERM: the attributes in
    force before it started are written back, and it exits non-zero."""
    term = _Pty()
    ready = tmp_path / "cli.started"
    proc = subprocess.Popen(
        [sys.executable, "-c", ATTACHED_DRIVER, RAW_CLI, str(ready)],
        stdin=term.slave, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, env=_clean_env(tmp_path))
    try:
        assert _wait_for(lambda: ready.is_file() and not term.canonical(), 20, proc), (
            "harness: the attached CLI never put the terminal in raw mode")
        assert proc.poll() is None, "harness: the driver ended before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("the driver did not exit on SIGTERM (R8f.10: it must not "
                        "ignore it)")
        assert term.attrs() == term.before, (
            "SIGTERM left the terminal as the CLI had set it: the attributes "
            "saved before the attached run were not written back")
        assert code != 0, f"the driver exited {code} on SIGTERM; R8f.10 wants non-zero"
    finally:
        _reap_group(proc)
        term.close()


def test_p0_r8f_10_sigterm_during_the_team_picker_restores_the_terminal(tmp_path):
    """`init-agent` with two teams and a terminal shows the picker, which puts
    the terminal in cbreak mode on its first key read. SIGTERM there must put
    the saved attributes back and still end the process, non-zero."""
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    ProjectPaths(root).ensure()
    term = _Pty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "multiagents.cli", "--path", str(root), "init-agent"],
        stdin=term.slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, env=_clean_env(tmp_path), cwd=root)
    try:
        assert _wait_for(lambda: not term.canonical(), 30, proc), (
            "harness: the picker never took the terminal out of canonical mode "
            f"(exit {proc.poll()})")
        assert proc.poll() is None, "harness: init-agent ended before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("init-agent did not exit on SIGTERM during the picker")
        assert term.attrs() == term.before, (
            "SIGTERM during the picker left the terminal in cbreak mode")
        assert code != 0, f"init-agent exited {code} on SIGTERM; R8f.10 wants non-zero"
    finally:
        _reap_group(proc)
        term.close()


# ============================================================= P0-R8f.11 ==
# A captured action never outlives its caller.

SLOW_ACTION = """#!/bin/sh
echo $$ > "$SLOW_PIDFILE"
sleep 60 &
wait
"""


class _Interrupt(KeyboardInterrupt):
    pass


class _Other(RuntimeError):
    pass


def _group_members(pgid: int) -> list[int]:
    """Every process, zombies included, whose process group is `pgid`."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[2]) == pgid:
            found.append(int(entry.name))
    return found


@pytest.mark.parametrize("exc_type", [_Interrupt, _Other],
                         ids=["keyboard_interrupt", "any_other_exception"])
def test_p0_r8f_11_an_interrupted_action_leaves_no_process_of_its_group(
        tmp_path, monkeypatch, exc_type):
    config_dir = tmp_path / "cfg"
    (config_dir / "providers").mkdir(parents=True)
    (config_dir / "providers" / "slow.sh").write_text(SLOW_ACTION)
    pidfile = tmp_path / "action.pid"
    # EV-R2: through the provider's `env:` block, not the ambient environment.
    provider = types.SimpleNamespace(script_name="slow.sh",
                                     env={"SLOW_PIDFILE": str(pidfile)})
    raised = exc_type("interrupted while waiting for the provider script")

    def interrupt(*_):
        raise raised

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(exc_type) as caught:
            scripts.run_action("slow", provider, object(), "compact", config_dir,
                               timeout=60)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert caught.value is raised, "the exception did not propagate unchanged"
    assert pidfile.is_file(), "harness: the action never started"
    pgid = int(pidfile.read_text().strip())
    try:
        # Grandchildren are reaped by init once killed; allow it a moment. The
        # script itself is the caller's child, so it must already be reaped.
        _wait_for(lambda: not _group_members(pgid), 3)
        left = _group_members(pgid)
        assert left == [], (
            f"process(es) {left} of the action's group outlived the interrupted "
            f"run_action")
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


# ============================================================= P0-R8f.12 ==
# A user's own exit wins over a pending compaction.

def _extended_fake() -> str:
    """The R8f fake CLI, plus: it records its pid, and a launch whose control
    entry names `exit_when` exits by itself as soon as that file exists."""
    fake = ic.FAKE.replace("{python}", sys.executable)
    record = '"env": env, **extra}'
    loop = "        if now - start >= life:\n            break"
    assert record in fake and loop in fake, "harness: the R8f fake has changed shape"
    fake = fake.replace(record, '"env": env, "pid": os.getpid(), **extra}')
    return fake.replace(loop, (
        "        if now - start >= life or (me.get(\"exit_when\") and "
        "pathlib.Path(me[\"exit_when\"]).exists()):\n            break"))


def _dead(pid: int) -> bool:
    """Exited: gone, or a zombie nobody has reaped yet."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat[stat.rindex(")") + 2:].split()[0] == "Z"


class _ExitsWhenTold(ic._Tty):
    """stdin, as a terminal, that also makes the CLI exit by itself at a
    chosen moment.

    The driver consults stdin on each poll while a compaction is pending (R8f
    does not apply without a terminal). On the first such poll after `at`
    seconds past the announcement, this tells the CLI to exit. With `settle`,
    it then waits until the CLI has exited, and until `hold` seconds past the
    announcement, before letting the poll carry on.

    The polls run in step with the announcement, so the one that finds the
    grace period over comes just after it. With `at` a little before the
    grace period ends and `hold` a little after it, that poll is this one, and
    the CLI's own exit lands exactly between "the grace period has run out"
    and "the driver stops it": the window the review found, which is otherwise
    a race no test could hit reliably.
    """

    def __init__(self, s: ic.Session, flag: Path, at: float, settle: bool = False,
                 hold: float = 0.0):
        self.s, self.flag, self.at, self.settle, self.hold = s, flag, at, settle, hold
        self.fired = False

    def isatty(self) -> bool:
        if not self.fired:
            scheduled = self.s.events("compact_scheduled")
            if scheduled and time.time() >= scheduled[0]["t"] + self.at:
                self.fired = True
                self.flag.touch()
                if self.settle:
                    _wait_for(lambda: bool(self.s.calls("exit"))
                              and _dead(self.s.calls("exit")[0]["pid"]), 10)
                    rest = scheduled[0]["t"] + self.hold - time.time()
                    if rest > 0:
                        time.sleep(rest)
        return True


@pytest.fixture
def session(tmp_path, monkeypatch, capsys):
    def build(**kwargs) -> ic.Session:
        s = ic.Session(tmp_path, monkeypatch, capsys, **kwargs)
        s.script.write_text(_extended_fake())
        return s
    return build


@pytest.mark.parametrize("own_exit", [0, 1], ids=["exit_0", "exit_1"])
def test_p0_r8f_12_a_cli_that_exits_as_the_grace_period_expires_is_not_compacted(
        session, monkeypatch, tmp_path, own_exit):
    """R8f.12's verification: the CLI exits by itself at the moment the grace
    period expires. No `compact` action runs, the session is not relaunched,
    and the driver ends it as it would with no compaction pending: 0 for a
    normal exit, and for exit 1 the crash path (not retried by default)."""
    s = session()
    s.reading(ic.OVER)
    flag = tmp_path / "exit.now"
    tty = _ExitsWhenTold(s, flag, at=ic.GRACE - 0.05, settle=True,
                         hold=ic.GRACE + 0.02)
    monkeypatch.setattr(sys, "stdin", tty)
    code, out = s.run([{"life": ic.STOPPED_BY, "exit_when": str(flag),
                        "exit": own_exit}, {"life": 0}])
    if not tty.fired:
        pytest.skip("inconclusive: the driver never consulted stdin after the "
                    "grace period ran out, so the CLI's exit could not be placed "
                    "inside the stop window")
    assert s.calls("exit") and s.calls("exit")[0]["n"] == 1, (
        "harness: the CLI did not exit by itself")
    assert s.calls("compact") == [], (
        "the driver compacted a session whose CLI had exited by itself")
    assert s.events("compacted") == [] and s.events("compact_failed") == []
    assert len(s.calls("launch")) == 1, (
        f"the session was relaunched after the user's own exit: {s.seq()}")
    assert code == (0 if own_exit == 0 else 1), out


def test_p0_r8f_12_a_cli_that_exits_during_the_grace_period_is_not_compacted(
        session, monkeypatch, tmp_path):
    """The "announced" half of R8f.12: the CLI exits 0 by itself while the
    compaction is announced and not yet due."""
    s = session()
    s.reading(ic.OVER)
    flag = tmp_path / "exit.now"
    monkeypatch.setattr(sys, "stdin",
                        _ExitsWhenTold(s, flag, at=ic.GRACE / 2))
    code, _ = s.run([{"life": ic.STOPPED_BY, "exit_when": str(flag)}, {"life": 0}])
    assert len(s.events("compact_scheduled")) == 1, "harness: nothing was announced"
    assert s.calls("compact") == []
    assert len(s.calls("launch")) == 1, f"relaunched after the user's exit: {s.seq()}"
    assert code == 0


# ============================================================= P0-R8f.13 ==
# The driver's other limits parse safely: malformed -> the shipped default.

def test_p0_r8f_13_the_defaults_this_section_relies_on_are_shipped():
    assert SHIPPED_LIMITS["restart_attempts"] == 5
    assert SHIPPED_LIMITS["restart_delay_seconds"] == 60
    assert SHIPPED_LIMITS["limit_max_waits"] == 12
    assert SHIPPED_LIMITS["compact_at_tokens"] == 120000
    assert SHIPPED_LIMITS["context_wind_down_tokens"] == 150000
    assert SHIPPED_LIMITS["compact_timeout_seconds"] == 180


# --- restart_attempts ---------------------------------------------------

LOST_FIVE_TIMES = [{"exit": 129}] * 5 + [{"exit": 0}]


@_malformed(strings=("five",))
def test_p0_r8f_13_a_malformed_restart_attempts_is_the_default_five(session, value):
    """Five terminal losses in a row are each retried (1/5 .. 5/5); the sixth
    launch exits 0 and the driver ends normally."""
    s = session(compact_at=0, limits={"restart_attempts": value})
    code, out = s.run(LOST_FIVE_TIMES)
    assert code == 0, f"a malformed restart_attempts ended the run ({code}):\n{out}"
    assert "(1/5)" in out and "(5/5)" in out, (
        f"the retries were not counted against the default 5:\n{out}")
    assert len(s.calls("launch")) == 6, s.seq()


# --- restart_delay_seconds ----------------------------------------------

@pytest.fixture
def ctrl_c_after():
    """Deliver a Ctrl-C (KeyboardInterrupt) into the test after `seconds`."""
    previous = signal.getsignal(signal.SIGALRM)

    def arm(seconds: float) -> None:
        def interrupt(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGALRM, interrupt)
        signal.setitimer(signal.ITIMER_REAL, seconds)

    yield arm
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous)


@_malformed(strings=("1m",))
def test_p0_r8f_13_a_malformed_restart_delay_is_the_default_sixty(
        session, ctrl_c_after, value):
    """After a terminal loss the driver says it retries in 60s and waits:
    nothing is relaunched within the first seconds. Ctrl-C during the wait
    ends the run, as the retry line offers."""
    s = session(compact_at=0, limits={"restart_delay_seconds": value})
    ctrl_c_after(2.0)
    code, out = s.run([{"exit": 129}, {"exit": 0}])
    assert "Retrying in 60s" in out, (
        f"the retry was not announced with the default 60s delay:\n{out}")
    assert len(s.calls("launch")) == 1, (
        f"relaunched within 2s: the default 60s delay was not kept ({s.seq()})")
    assert code == 0, out


def test_p0_r8f_13_an_explicit_zero_restart_delay_means_no_wait(session):
    """0 is a value here (retry at once), not a malformed one."""
    s = session(compact_at=0, limits={"restart_delay_seconds": 0})
    code, _ = s.run([{"exit": 129}, {"exit": 0}])
    assert code == 0
    assert len(s.calls("launch")) == 2
    assert s.elapsed < 10


# --- limit_max_waits ----------------------------------------------------

LIMIT_MARKERS = [{"match": "usage limit reached", "resets": True,
                  "detail": "usage window"}]


def _limited(s: ic.Session) -> None:
    """The session's transcript ends with the CLI's own usage-limit message,
    so every launch is stopped for a limit that resets."""
    ch.write_transcript(s.transcript, [ch.user("go"),
                                       ch.limit_message("Claude usage limit reached.")])


def _limit_session(session, value):
    return session(compact_at=0, limit_markers=LIMIT_MARKERS,
                   limits={"limit_max_waits": value, "limit_wait_seconds": 0.001})


def test_p0_r8f_13_an_explicit_limit_max_waits_is_honoured(session):
    """Control for the malformed cases below: two waits, then it gives up."""
    s = _limit_session(session, 2)
    _limited(s)
    code, out = s.run([{"life": ic.STOPPED_BY}])
    assert code == 3, out
    assert len(s.calls("launch")) == 3, s.seq()


@_malformed(strings=("twelve",))
def test_p0_r8f_13_a_malformed_limit_max_waits_is_the_default_twelve(session, value):
    """A limit that keeps coming back is waited out twelve times; the
    thirteenth stop ends the run with exit 3."""
    s = _limit_session(session, value)
    _limited(s)
    code, out = s.run([{"life": ic.STOPPED_BY}])
    assert code == 3, f"a malformed limit_max_waits ended the run with {code}:\n{out}"
    assert len(s.calls("launch")) == 13, (
        f"expected the first launch and 12 waits' relaunches, saw "
        f"{len(s.calls('launch'))}")
    assert "12 waits" in out, out


def test_p0_r8f_13_a_malformed_limit_max_waits_does_not_crash_a_lost_terminal(session):
    """The budget is read on the retry path too, with no limit in sight."""
    s = session(compact_at=0, limits={"limit_max_waits": "twelve"})
    code, out = s.run([{"exit": 129}, {"exit": 0}])
    assert code == 0, out
    assert len(s.calls("launch")) == 2


# --- compact_at_tokens, in the driver -----------------------------------

DEFAULT_COMPACT_AT = 120_000


@_malformed(strings=("120k",))
def test_p0_r8f_13_a_malformed_compact_at_is_the_default_in_the_attached_driver(
        session, value):
    """Superseding "malformed -> off": over the shipped 120000 is compacted."""
    s = session(compact_at=value)
    s.reading(DEFAULT_COMPACT_AT + 10_000)
    code, _ = s.run(ic.stopped_then())
    ic.assert_stop_compact_resume(s)
    assert code == 0


def test_p0_r8f_13_a_malformed_compact_at_is_the_default_not_a_lower_value(session):
    """Under the shipped 120000 is not compacted: the fallback is the default,
    not any small number."""
    s = session(compact_at="120k")
    s.reading(DEFAULT_COMPACT_AT - 10_000)
    code, _ = s.run(ic.KEEP)
    assert code == 0
    ic.assert_not_stopped(s)


@_malformed(strings=("120k",))
def test_p0_r8f_13_a_malformed_compact_at_is_the_default_in_the_headless_loop(
        session, value):
    s = session(compact_at=value)
    s.reading(DEFAULT_COMPACT_AT + 10_000)
    s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
                                 "events": str(s.paths.events_file),
                                 "launches": [{"life": 0}],
                                 "compact": {"exit": 0, "stdout": ic.FIGURES + "\n"}}))
    driver._supervise(s.paths, s.config, "orchestrator", s.spec, s.provider, object(),
                      dict(s.context), 1)
    assert s.seq() == ["launch", "compact"], (
        f"a malformed compact_at_tokens did not compact over the default: {s.seq()}")


# --- compact_timeout_seconds, in the driver -----------------------------

@_malformed(strings=("3m",))
def test_p0_r8f_13_a_malformed_compact_timeout_is_the_default(session, value):
    """A compaction that takes 1.5s is well inside the default 180s: it
    succeeds, rather than timing out against a zero or tiny fallback."""
    s = session(limits={"compact_timeout_seconds": value})
    s.reading(ic.OVER)
    code, _ = s.run(ic.stopped_then(),
                    compact={"exit": 0, "sleep": 1.5, "stdout": ic.FIGURES + "\n"})
    assert code == 0
    assert len(s.events("compacted")) == 1
    assert s.events("compact_failed") == []


# --- context_wind_down_tokens and compact_at_tokens, in the server ------

DEFAULT_WIND_DOWN = 150_000


def _block() -> dict:
    return server.budget_status()["context"]


@_malformed(strings=("150k",))
def test_p0_r8f_13_a_malformed_wind_down_is_the_default_in_the_server(served, value):
    p = served(wind_down=value)
    ch.write_transcript(p.transcript, [ch.request(DEFAULT_WIND_DOWN - 1)])
    assert _block().get("wind_down_at") == DEFAULT_WIND_DOWN
    assert "context_wind_down" not in server.agent_tree(), (
        "the notice fired under the default threshold")
    ch.write_transcript(p.transcript, [ch.request(DEFAULT_WIND_DOWN - 1),
                                       ch.request(DEFAULT_WIND_DOWN + 1)])
    assert "context_wind_down" in server.agent_tree(), (
        "no notice over the default threshold: a malformed value switched it off")


@_malformed(strings=("120k",))
def test_p0_r8f_13_a_malformed_compact_at_is_the_default_in_the_server(served, value):
    served(compact_at=value)
    assert _block().get("compact_at") == DEFAULT_COMPACT_AT


@pytest.mark.parametrize("key", ["context_wind_down_tokens", "compact_at_tokens"])
def test_p0_r8f_13_an_explicit_zero_is_still_off_in_the_server(served, key):
    p = served(**({"wind_down": 0} if key == "context_wind_down_tokens"
                  else {"compact_at": 0}))
    ch.write_transcript(p.transcript, [ch.request(900_000)])
    block = _block()
    field = "wind_down_at" if key == "context_wind_down_tokens" else "compact_at"
    assert block.get(field) == 0
    if key == "context_wind_down_tokens":
        assert "context_wind_down" not in server.agent_tree()

