"""SL — the phase 7 part 1 suite must not leave scheduler processes behind.

Contract: `context/specs/test-process-leak.md` (SL-R3, SL-R4; SL-R1/R2/R5 are
verified by SL-R3 and by running the scoped suite).

How the suite tells "ours" from everyone else's: each check runs an inner
pytest session (a subprocess) with a unique marker variable in its environment
and a unique `--basetemp` of its own. A process belongs to that session if the
marker is in its environment, or its basetemp appears in its command line,
working directory or environment. The real host scheduler of this project and
other agents' concurrent suites carry neither, so they are never counted.
Nothing is killed by pattern: leftovers found are killed by exact pid, and
only after being reported, to keep the machine clean when the check is red.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MARKER_VAR = "SL_SESSION_MARKER"
SCOPE_GLOBS = ("test_nc_*.py", "test_c23_*.py", "test_pc_*.py")
SCHEDULER_RE = re.compile(r"scheduler")
SETTLE_SECONDS = 5          # grace for a just-signalled process to disappear
SMALL_RUN_TIMEOUT = 120
SCOPE_RUN_TIMEOUT = 2400
# Outside any git repository (see AGENTS.md); real disk, not the shared tmpfs.
BASE = Path("/var/tmp") if Path("/var/tmp").is_dir() else Path(tempfile.gettempdir())


def _read(pid: int, name: str) -> bytes:
    try:
        return (Path("/proc") / str(pid) / name).read_bytes()
    except OSError:
        return b""


def _state(pid: int) -> str:
    m = re.search(rb"\) (\w)", _read(pid, "stat"))
    return m.group(1).decode() if m else "?"


def session_processes(marker: str, basetemp: str) -> list[tuple[int, str]]:
    """Live scheduler/worker processes belonging to the session `marker`/`basetemp`."""
    found = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid = int(entry)
        cmd = _read(pid, "cmdline").replace(b"\0", b" ").decode(errors="replace").strip()
        if not cmd or "pytest" in cmd or not SCHEDULER_RE.search(cmd):
            continue
        if _state(pid) in ("Z", "X", "?"):
            continue
        env = _read(pid, "environ").decode(errors="replace")
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        ours = (f"{MARKER_VAR}={marker}" in env.split("\0")
                or basetemp in cmd or basetemp in cwd or basetemp in env)
        if ours:
            found.append((pid, cmd))
    return found


def settled_leaks(marker: str, basetemp: str) -> list[tuple[int, str]]:
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        leaks = session_processes(marker, basetemp)
        if not leaks or time.monotonic() >= deadline:
            return leaks
        time.sleep(0.1)


def kill_by_pid(leaks) -> None:
    for pid, _ in leaks:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def session():
    marker = uuid.uuid4().hex
    base = BASE / f"sl-{uuid.uuid4().hex[:12]}"
    base.mkdir()
    sess = {"marker": marker, "base": base, "basetemp": str(base / "bt")}
    yield sess
    kill_by_pid(session_processes(marker, str(base)))
    shutil.rmtree(base, ignore_errors=True)


def run_inner(session, args, timeout):
    env = dict(os.environ, **{MARKER_VAR: session["marker"]}, PYTHONPATH="src")
    for name in list(env):
        if name.startswith(("PYTEST_", "MULTIAGENTS_")):
            del env[name]
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={session['basetemp']}", *args],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Our own inner session's group only (exact pgid), never a pattern.
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        pytest.fail(f"the inner pytest session timed out after {timeout}s:\n{out[-1500:]}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def assert_no_leaks(session, what: str) -> None:
    leaks = settled_leaks(session["marker"], str(session["base"]))
    if leaks:
        kill_by_pid(leaks)
        listing = "\n".join(f"  pid {pid}: {cmd}" for pid, cmd in leaks)
        pytest.fail(f"{what} left {len(leaks)} scheduler/worker process(es) "
                    f"behind:\n{listing}")


# ------------------------------------------------- the detector itself (SL-R3)

def _decoy(session, argv_tail: str, *, marked: bool):
    env = dict(os.environ)
    env.pop(MARKER_VAR, None)
    if marked:
        env[MARKER_VAR] = session["marker"]
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"] + argv_tail.split(),
        env=env, cwd="/", start_new_session=True)
    return proc


@pytest.fixture
def decoys(session):
    """Start decoys through `start(...)`; each is reaped at teardown from the
    moment it exists, even if a later decoy fails to start or the test fails."""
    started = []

    def start(argv_tail: str, *, marked: bool):
        proc = _decoy(session, argv_tail, marked=marked)
        started.append(proc)
        return proc

    yield start
    for p in started:
        p.kill()
        p.wait()


def test_sl_r3_the_check_sees_a_leaked_scheduler_and_a_leaked_worker(session, decoys):
    sched = decoys("scheduler start --foreground", marked=True)
    worker = decoys("-m multiagents.scheduler.worker", marked=True)
    time.sleep(0.2)
    pids = {p for p, _ in session_processes(session["marker"], str(session["base"]))}
    assert {sched.pid, worker.pid} <= pids
    with pytest.raises(pytest.fail.Exception) as exc:
        assert_no_leaks(session, "decoys")
    assert str(sched.pid) in str(exc.value) and "scheduler start" in str(exc.value)


def test_sl_r3_the_check_ignores_scheduler_processes_that_are_not_the_sessions(session, decoys):
    decoys("scheduler start --foreground", marked=False)
    time.sleep(0.2)
    assert session_processes(session["marker"], str(session["base"])) == []


def assert_inner_ran(res) -> int:
    """The inner pytest ran its tests (exit 0, or 1 for unrelated reds, which
    SL-R5 covers separately); returns the number of tests that passed."""
    tail = f"exit code {res.returncode}\nstderr:\n{res.stderr[-2000:]}\nstdout tail:\n{res.stdout[-3000:]}"
    assert res.returncode in (0, 1), f"the inner pytest session did not run its tests: {tail}"
    m = re.search(r"(\d+) passed", res.stdout.splitlines()[-1] if res.stdout.strip() else "")
    assert m and int(m.group(1)) > 0, f"no test passed in the inner pytest session: {tail}"
    return int(m.group(1))


# ----------------------------------------------------------------------- SL-R4

def _write_failing_inner(session, imports: str, body: str) -> Path:
    path = session["base"] / "test_sl_inner_failing.py"
    path.write_text(textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(REPO / 'tests')!r})
        {imports}

        {textwrap.indent(textwrap.dedent(body), '        ').lstrip()}
        """))
    return path


def test_sl_r4_a_test_that_fails_while_the_scheduler_runs_leaves_nothing_behind(session):
    inner = _write_failing_inner(session, "from test_nc_m2_lifecycle import w  # noqa: F401", """
        def test_fails_with_scheduler_up(w):
            w.start_scheduler()
            assert False, "deliberate failure while the scheduler is running"
        """)
    res = run_inner(session, [str(inner), "-p", "no:randomly", "-c", str(REPO / "pyproject.toml"),
                              "--rootdir", str(REPO), "--confcutdir", str(REPO / "tests")],
                    SMALL_RUN_TIMEOUT)
    assert "deliberate failure" in res.stdout, res.stdout[-2000:] + res.stderr[-2000:]
    assert "1 failed" in res.stdout, res.stdout[-2000:]
    assert_no_leaks(session, "a failing test with a running scheduler")


def test_sl_r4_a_test_that_errors_in_its_body_with_a_running_scheduler_and_a_run_leaves_nothing(session):
    inner = _write_failing_inner(session, "from test_nc_m2_lifecycle import w  # noqa: F401", """
        def test_raises_with_a_live_run(w):
            w.provider("pcfx", max_concurrent=1)
            w.agent("pcworker", "pcfx")
            w.start_scheduler()
            node = w.simple("A", fx={"gate": "ga"})
            w.wait_running(node)
            raise RuntimeError("deliberate error with a live run")
        """)
    res = run_inner(session, [str(inner), "-c", str(REPO / "pyproject.toml"),
                              "--rootdir", str(REPO), "--confcutdir", str(REPO / "tests")],
                    SMALL_RUN_TIMEOUT)
    assert "deliberate error" in res.stdout, res.stdout[-2000:] + res.stderr[-2000:]
    assert_no_leaks(session, "an erroring test with a live run")


# ----------------------------------------------------------------------- SL-R3

def test_sl_r3_the_migration_module_alone_leaves_no_scheduler_or_worker_process(session):
    """Fast subset of the check below: the module that leaked `scheduler.worker`
    processes (test_nc_r19_*) on main @ bff6ab5."""
    res = run_inner(session, ["tests/test_nc_m2_migration.py", "-n", "2"], SMALL_RUN_TIMEOUT)
    assert_inner_ran(res)
    assert_no_leaks(session, "tests/test_nc_m2_migration.py")


@pytest.mark.skipif(
    os.environ.get("MULTIAGENTS_SL_FULL") != "1",
    reason="whole-scope leak check; set MULTIAGENTS_SL_FULL=1 (about 13 min)",
)
def test_sl_r3_running_the_scoped_suite_leaves_no_scheduler_or_worker_process(session):
    files = sorted(str(p.relative_to(REPO)) for g in SCOPE_GLOBS
                   for p in (REPO / "tests").glob(g))
    assert files, "no scoped test files found"
    # Never recurse into this file (it is not in scope, but be explicit).
    files = [f for f in files if "test_sl_" not in f]
    before = session_processes(session["marker"], str(session["base"]))
    assert before == []
    res = run_inner(session, [*files, "-n", "4"], SCOPE_RUN_TIMEOUT)
    assert_inner_ran(res)
    assert_no_leaks(session, "the scoped suite (tests/test_nc_*, test_c23_*, test_pc_*)")
