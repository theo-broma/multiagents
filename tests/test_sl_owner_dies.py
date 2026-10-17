"""SL part 2 — test processes die with their owner (SL-R6..SL-R9).

Contract: `context/specs/test-process-leak.md`, section "Part 2: the owner
dies". SL-R10 (no per-id outcome change, runtime +10 % at most) is verified by
the full suite run, not here.

THE SEAM. The marker's form is the implementer's choice, so everything about
it goes through `tests/support/scheduler_owner.py` (five names: `mark_env`,
`unmarked_env`, `marked_processes`, `sweep_dead_owners`; see its docstring).
The tests need nothing else of the marker. Everything else is observed from
the outside: `/proc`, the scheduler socket, the fixture providers' call logs.

How these tests stay safe on a shared machine: nothing is ever selected by a
name pattern. A test signals only (a) the pids it created, checked against
their start time, (b) the descendants of a daemon it started, and (c) through
the code under test. Anything a test might leave behind is registered with the
`reaper` fixture the moment it exists and killed at teardown, pass or fail.

Time: SL-R7's bound is real (monotonic) seconds, so the four "owner dies"
tests each wait up to the bound; they are the genuinely slow ones and are
kept to one per behaviour. A red run of them costs the bound each.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import scheduler_owner as owner_seam  # noqa: E402
from nc_fixture.agent import alive  # noqa: E402
from nc_fixture.world import World  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
BASE = Path("/var/tmp") if Path("/var/tmp").is_dir() else Path("/tmp")

OWNER_DEATH_BOUND = 10      # SL-R7: seconds of real time
BOUND_SLACK = 2             # scheduling jitter of the checker itself under -n 4
INNER_READY_TIMEOUT = 45    # the inner pytest owner reaching its running worker
LIVE_OWNER_SETTLE = 4       # how long "nothing happens to workers" is watched
SWEEP_TIMEOUT = 12          # SL-R9: a planted dead-owner process disappearing
SIGNAL_TIMEOUT = 5          # a SIGKILLed process disappearing
RUN_TIMEOUT = 10            # a node reaching a state
INNER_SESSION_TIMEOUT = 90


# ------------------------------------------------------------------ /proc

def _stat(pid: int):
    """(state, ppid, start) of `pid`, or None when it does not exist."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = raw.rsplit(")", 1)[1].split()
    return rest[0], int(rest[1]), int(rest[19])


def start_of(pid: int) -> int:
    st = _stat(pid)
    assert st, f"pid {pid} does not exist"
    return st[2]


def live(pid: int, start: int | None = None) -> bool:
    """Running or sleeping, not a zombie, and (when given) the same process."""
    st = _stat(pid)
    if not st or st[0] in ("Z", "X"):
        return False
    return start is None or st[2] == start


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def descendants(pid: int) -> list[int]:
    kids: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            st = _stat(int(entry))
            if st:
                kids.setdefault(st[1], []).append(int(entry))
    out, todo = [], [pid]
    while todo:
        for kid in kids.get(todo.pop(), []):
            out.append(kid)
            todo.append(kid)
    return out


def worker_pids(root: Path) -> list[int]:
    """`scheduler.worker` processes whose argv names this test's own unique
    project root (never anyone else's)."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or not live(int(entry)):
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if "multiagents.scheduler.worker" in argv and str(root) in argv:
            out.append(int(entry))
    return out


def poll(pred, timeout: float, step: float = 0.1):
    end = time.monotonic() + timeout
    while True:
        value = pred()
        if value or time.monotonic() >= end:
            return value
        time.sleep(step)


def wait_gone(procs: list[tuple[int, int]], timeout: float) -> list[tuple[int, int]]:
    """Wait until none of `(pid, start)` is live; the ones still live."""
    def survivors():
        return [(p, s) for p, s in procs if live(p, s)]
    poll(lambda: not survivors(), timeout)
    return survivors()


def describe(procs) -> str:
    return "\n".join(f"  pid {p}: {cmdline(p)}" for p, _ in procs)


def clean_env() -> dict[str, str]:
    """`os.environ` without the marker. Falls back to a plain copy while the
    seam is a stub, so that tests stay red on their own assertions."""
    try:
        return owner_seam.unmarked_env()
    except NotImplementedError:
        return dict(os.environ)


# ---------------------------------------------------------------- fixtures

class Reaper:
    def __init__(self):
        self.popens: list[subprocess.Popen] = []
        self.procs: list[tuple[int, int]] = []

    def popen(self, argv, **kw) -> subprocess.Popen:
        kw.setdefault("stdin", subprocess.DEVNULL)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        kw.setdefault("start_new_session", True)
        proc = subprocess.Popen(argv, **kw)
        self.popens.append(proc)
        self.procs.append((proc.pid, start_of(proc.pid)))
        return proc

    def track(self, pid: int) -> None:
        st = _stat(pid)
        if st:
            self.procs.append((pid, st[2]))

    def close(self) -> None:
        for pid, start in self.procs:
            if live(pid, start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for proc in self.popens:
            try:
                proc.wait(timeout=SIGNAL_TIMEOUT)
            except Exception:                               # noqa: BLE001
                pass


@pytest.fixture
def reaper():
    r = Reaper()
    yield r
    r.close()


@pytest.fixture
def sleeper_owner(reaper):
    """A live process to act as somebody else's owner."""
    proc = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)"])
    return proc.pid, start_of(proc.pid)


def make_world(tmp_path: Path, monkeypatch, name: str = "w") -> World:
    root = tmp_path / name
    root.mkdir()
    world = World(root, monkeypatch)
    world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    return world


def run_worker(world: World, tag: str = "A") -> tuple[str, int, list[tuple[int, int]]]:
    """Start the scheduler and a node whose run is held at a gate; returns
    (node id, daemon pid, [(pid, start)] of the daemon and everything below it)."""
    daemon = world.start_scheduler()
    node = world.simple(tag, agent="pcworker", fx={"gate": "g" + tag})
    world.wait_running(node)
    assert poll(lambda: worker_pids(world.root), RUN_TIMEOUT), "no scheduler.worker process appeared"
    return node, daemon, tree_of(daemon)


def tree_of(daemon: int) -> list[tuple[int, int]]:
    return [(p, start_of(p)) for p in [daemon, *descendants(daemon)]]


# ------------------------------------------------------------------- SL-R6

def test_sl_r6_a_marked_daemon_and_its_worker_carry_the_owner_and_the_project(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    try:
        _node, daemon, tree = run_worker(w)
        workers = set(worker_pids(w.root))
        carried = owner_seam.marked_processes(os.getpid(), w.root)
        assert daemon in carried, "the daemon does not carry this pytest process's marker"
        assert workers <= carried, f"workers without the marker: {workers - carried}"
        # the owner is the pytest process itself, not whoever launched it
        assert not ({daemon} | workers) & owner_seam.marked_processes(os.getppid())
        # and the marker names the project
        assert not ({daemon} | workers) & owner_seam.marked_processes(os.getpid(), tmp_path / "elsewhere")
    finally:
        w.close()


def test_sl_r6_every_process_of_a_workers_run_carries_the_marker(tmp_path, monkeypatch):
    """The fixture agent is the run's wrapper descendant: it must be reachable
    by the marker too, so teardown and the sweep need no parent chain."""
    w = make_world(tmp_path, monkeypatch)
    try:
        _node, daemon, tree = run_worker(w)
        below = {p for p, _ in tree if p != daemon}
        assert below, "the daemon has no descendants"
        assert below <= owner_seam.marked_processes(os.getpid(), w.root)
    finally:
        w.close()


def test_sl_r6_an_unmarked_daemon_and_its_worker_carry_no_marker(tmp_path, monkeypatch, reaper):
    w = make_world(tmp_path, monkeypatch)
    try:
        _node, daemon, tree = start_unmarked(w, reaper)
        pids = {p for p, _ in tree}
        assert not pids & owner_seam.marked_processes(os.getpid())
        assert not pids & owner_seam.marked_processes(os.getppid())
    finally:
        w.close()


def start_unmarked(w: World, reaper: Reaper):
    """Like `run_worker`, but the daemon is started by hand with no marker."""
    w.write_config()
    proc = reaper.popen([sys.executable, "-c", "import sys; from multiagents.cli import main; sys.exit(main(sys.argv[1:]))",
                         "--path", str(w.root), "scheduler", "start", "--foreground"],
                        cwd=w.root, env=dict(clean_env(), MULTIAGENTS_PROJECT=str(w.root)))
    assert poll(lambda: w.sock_answers(), 12), "the unmarked scheduler did not come up"
    daemon = proc.pid
    node = w.simple("A", agent="pcworker", fx={"gate": "gA"})
    w.wait_running(node)
    assert poll(lambda: worker_pids(w.root), RUN_TIMEOUT)
    # This daemon is deliberately unmarked: World.close must leave it alone.
    # The test owns its foreground Popen and the descendants of that handle.
    tree = tree_of(proc.pid)
    reaper.procs.extend(tree)
    return node, daemon, tree


def test_sl_r6_an_unmarked_daemon_still_leaves_its_worker_running_and_adopts_it(tmp_path, monkeypatch, reaper):
    """Production behaviour is unchanged without the marker (also today's)."""
    w = make_world(tmp_path, monkeypatch)
    try:
        node, daemon, _tree = start_unmarked(w, reaper)
        workers = worker_pids(w.root)
        w.stop_scheduler()
        assert wait_gone([(daemon, start_of(daemon))] if live(daemon) else [], SIGNAL_TIMEOUT) == []
        time.sleep(LIVE_OWNER_SETTLE)
        assert all(live(p) for p in workers), "an unmarked daemon's stop took its worker with it"
        w.start_scheduler()
        assert w.state(node) == "running"
        w.providers["pcfx"].open_gate("gA")
        w.wait_state(node, "done", timeout=RUN_TIMEOUT)
    finally:
        w.close()


# ------------------------------------------------------------------- SL-R7

INNER_OWNER = '''\
import json, os, sys, time
from pathlib import Path
TESTS = {tests!r}
sys.path.insert(0, TESTS)
sys.path.insert(0, TESTS + "/support")
from conftest import (_caller_environment_is_invisible, _machine_state_is_disposable,  # noqa
                      _no_catalog_fetch, _no_provider_subprocesses, _no_host_cli_budget)
from nc_fixture.world import World


def stat(pid):
    try:
        rest = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        return int(rest[1]), int(rest[19])
    except OSError:
        return None


def below(pid):
    kids = {{}}
    for e in os.listdir("/proc"):
        if e.isdigit():
            s = stat(int(e))
            if s:
                kids.setdefault(s[0], []).append(int(e))
    out, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.append(k)
            todo.append(k)
    return out


def test_owner_holds_a_marked_daemon_with_a_running_worker(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    fx = w.provider("pcfx", max_concurrent=1)
    w.agent("pcworker", "pcfx")
    daemon = w.start_scheduler()
    a = w.simple("A", agent="pcworker", fx={{"gate": "ga"}})
    w.wait_running(a)
    if os.environ.get("SL_QUEUE_B"):
        w.simple("B", agent="pcworker")
        time.sleep(2.5)
    procs = [[p, stat(p)[1]] for p in [daemon, *below(daemon)] if stat(p)]
    info = {{"owner": os.getpid(), "daemon": daemon, "procs": procs, "root": str(w.root),
            "calls": str(fx.dir / "calls.jsonl")}}
    out = Path(os.environ["SL_OUT"])
    out.with_suffix(".tmp").write_text(json.dumps(info))
    out.with_suffix(".tmp").rename(out)
    time.sleep(600)
'''


def launch_owner(tmp_path: Path, reaper: Reaper, *, queue_b: bool = False):
    """A pytest process (the owner) holding a marked daemon with a running
    worker. Returns (Popen, info); everything it started is registered for
    teardown the moment the snapshot exists."""
    script = tmp_path / "test_sl_inner_owner.py"
    script.write_text(INNER_OWNER.format(tests=str(TESTS)))
    out = tmp_path / "inner.json"
    env = dict(clean_env(), PYTHONPATH="src", SL_OUT=str(out))
    for name in list(env):
        if name.startswith(("PYTEST_", "MULTIAGENTS_")):
            del env[name]
    if queue_b:
        env["SL_QUEUE_B"] = "1"
    base = BASE / f"sl2-{uuid.uuid4().hex[:12]}"       # outside any repository, no `ag-`
    proc = reaper.popen([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                         f"--basetemp={base}", "-c", str(REPO / "pyproject.toml"), "--rootdir", str(REPO),
                         str(script)],
                        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = poll(lambda: out.exists() or proc.poll() is not None, INNER_READY_TIMEOUT)
    if not out.exists():
        tail = proc.stdout.read().decode(errors="replace")[-2500:] if proc.poll() is not None else "(still running)"
        pytest.fail(f"the inner owner never reached its running worker (ok={ok}): {tail}")
    info = json.loads(out.read_text())
    for pid, _ in info["procs"]:
        reaper.track(pid)
    reaper.procs.extend((p, s) for p, s in info["procs"])
    assert info["owner"] == proc.pid
    return proc, info


def _owner_dies(tmp_path, reaper, *, sig, daemon_first=False, reap=True, queue_b=False):
    proc, info = launch_owner(tmp_path, reaper, queue_b=queue_b)
    procs = [tuple(x) for x in info["procs"]]
    assert len(procs) >= 3, f"expected daemon, worker and run below the owner, got {procs}"
    if daemon_first:
        os.kill(info["daemon"], signal.SIGKILL)
        assert wait_gone([procs[0]], SIGNAL_TIMEOUT) == []
    os.kill(proc.pid, sig)
    t0 = time.monotonic()
    if reap:
        proc.wait(timeout=SIGNAL_TIMEOUT)
    # a daemon killed first leaves a worker whose daemon is gone: it must not need it
    left = wait_gone(procs, OWNER_DEATH_BOUND + BOUND_SLACK)
    took = time.monotonic() - t0
    assert not left, (f"{len(left)} process(es) of the dead owner {proc.pid} still live "
                      f"{took:.1f}s after its death (bound {OWNER_DEATH_BOUND}s):\n{describe(left)}")
    stray = worker_pids(Path(info["root"]))
    assert not stray, f"scheduler.worker processes of the dead owner's project: {stray}"
    assert owner_seam.marked_processes(proc.pid) == set()
    return info


def test_sl_r7_a_sigkilled_owner_takes_its_daemon_worker_and_run_with_it(tmp_path, reaper):
    _owner_dies(tmp_path, reaper, sig=signal.SIGKILL)


def test_sl_r7_an_owner_that_is_a_zombie_counts_as_dead(tmp_path, reaper):
    """SIGKILLed and not reaped by its parent (this test): the owner is a zombie."""
    proc, info = launch_owner(tmp_path, reaper)
    procs = [tuple(x) for x in info["procs"]]
    os.kill(proc.pid, signal.SIGKILL)
    assert poll(lambda: (_stat(proc.pid) or ("?",))[0] == "Z", SIGNAL_TIMEOUT), "owner did not become a zombie"
    left = wait_gone(procs, OWNER_DEATH_BOUND + BOUND_SLACK)
    assert not left, f"a zombie owner's processes survive {OWNER_DEATH_BOUND}s:\n{describe(left)}"
    assert owner_seam.marked_processes(proc.pid) == set()


def test_sl_r7_a_sigtermed_owner_takes_its_daemon_worker_and_run_with_it(tmp_path, reaper):
    _owner_dies(tmp_path, reaper, sig=signal.SIGTERM)


def test_sl_r7_the_worker_ends_its_own_run_when_the_daemon_was_sigkilled_first(tmp_path, reaper):
    _owner_dies(tmp_path, reaper, sig=signal.SIGKILL, daemon_first=True)


def test_sl_r7_a_daemon_with_a_blocked_admission_launches_nothing_after_the_owner_died(tmp_path, reaper):
    """Node B waits behind A (max_concurrent 1). A's run ends because the owner
    died; the daemon must not admit B into the slot that frees."""
    info = _owner_dies(tmp_path, reaper, sig=signal.SIGKILL, queue_b=True)
    spawned = [json.loads(line) for line in Path(info["calls"]).read_text().splitlines() if line.strip()]
    assert [c["tag"] for c in spawned] == ["A"], f"runs launched: {[c['tag'] for c in spawned]}"


@pytest.mark.parametrize("how", ["stop", "kill9"])
def test_sl_r7_while_the_owner_lives_a_stopped_or_killed_daemon_leaves_workers_adoptable(tmp_path, monkeypatch, how):
    w = make_world(tmp_path, monkeypatch)
    try:
        node, daemon, _tree = run_worker(w)
        workers = worker_pids(w.root)
        run_tree = [(p, start_of(p)) for p in workers]
        if how == "stop":
            w.stop_scheduler()
        else:
            w.kill9()
        deadline = time.monotonic() + LIVE_OWNER_SETTLE
        while time.monotonic() < deadline:
            assert all(live(p, s) for p, s in run_tree), "the worker went away with its daemon while the owner lives"
            time.sleep(0.2)
        w.start_scheduler()
        assert w.state(node) == "running"
        w.providers["pcfx"].open_gate("gA")
        w.wait_state(node, "done", timeout=RUN_TIMEOUT)
    finally:
        w.close()


# ------------------------------------------------------------------- SL-R8

def test_sl_r8_closing_one_fixture_kills_its_daemon_and_worker_and_leaves_the_other(tmp_path, monkeypatch, reaper):
    one = make_world(tmp_path, monkeypatch, "one")
    two = make_world(tmp_path, monkeypatch, "two")
    try:
        _n1, _d1, tree1 = run_worker(one)
        n2, d2, tree2 = run_worker(two)
        reaper.procs.extend(tree1 + tree2)
        one.close()
        left = wait_gone(tree1, SIGNAL_TIMEOUT)
        assert not left, f"closing a fixture left its processes:\n{describe(left)}"
        assert all(live(p, s) for p, s in tree2), "closing one fixture killed the other's processes"
        assert two.status()["pid"] == d2
        assert two.state(n2) == "running"
    finally:
        two.close()
        one.close()


def test_sl_r8_teardown_never_touches_an_unmarked_lookalike_worker(tmp_path, monkeypatch, reaper):
    w = make_world(tmp_path, monkeypatch)
    try:
        _node, _daemon, _tree = run_worker(w)
        lookalike = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)",
                                  "multiagents.scheduler.worker", str(w.root)], env=clean_env())
        w.close()
        assert poll(lambda: not worker_pids(w.root) or worker_pids(w.root) == [lookalike.pid], SIGNAL_TIMEOUT)
        assert live(lookalike.pid), "teardown killed an unmarked process because its argv looked like a worker"
    finally:
        w.close()


def test_sl_r8_teardown_never_touches_a_worker_marked_for_another_live_owner(tmp_path, monkeypatch, reaper, sleeper_owner):
    w = make_world(tmp_path, monkeypatch)
    try:
        _node, _daemon, _tree = run_worker(w)
        other_pid, other_start = sleeper_owner
        foreign = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)",
                                "multiagents.scheduler.worker", str(w.root)],
                               env=dict(clean_env(), **owner_seam.mark_env(other_pid, other_start, w.root)))
        w.close()
        assert live(foreign.pid), "teardown killed a process marked for a different, live owner"
    finally:
        w.close()


def test_sl_r8_nc_harness_sched_teardown_after_a_worker_launched_leaves_no_worker(tmp_path, monkeypatch, reaper):
    from nc_harness import Sched
    s = Sched(tmp_path, monkeypatch)
    try:
        fake = s.p.fakes["acme"]
        fake.script(steps=[], tail_sleep=120)
        s.start()
        s.create(agent="worker", task="hold the slot")
        assert poll(lambda: worker_pids(s.root) and fake.spawns() >= 1, RUN_TIMEOUT), \
            "no scheduler.worker process and run appeared"
        daemon = s.pid()
        tree = tree_of(daemon)
        reaper.procs.extend(tree)            # a red Sched.close must not leave them
        assert len(tree) >= 3, f"daemon, worker and run expected, got {tree}"
        s.close()
        left = wait_gone(tree, SIGNAL_TIMEOUT)
        assert not left, f"Sched.close left processes behind:\n{describe(left)}"
        assert worker_pids(s.root) == []
    finally:
        s.close()


# ------------------------------------------------------------------- SL-R9

def plant(reaper: Reaper, root: Path, env: dict, *, shape: str = "worker",
          child_pidfile: Path | None = None) -> subprocess.Popen:
    """A process shaped like a scheduler worker (or daemon) carrying `env`. With
    `child_pidfile`, it also starts a child with NO marker and records its pid."""
    code = ("import json, subprocess, sys, time\n"
            "if len(sys.argv) > 4 and sys.argv[-1].endswith('.pid'):\n"
            "    env = json.load(open(sys.argv[-2]))\n"
            "    c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], env=env)\n"
            "    open(sys.argv[-1], 'w').write(str(c.pid))\n"
            "time.sleep(600)\n")
    tail = (["multiagents.scheduler.worker", str(root)] if shape == "worker" else
            ["multiagents.cli", "--path", str(root), "scheduler", "start", "--foreground"])
    if child_pidfile:
        envfile = child_pidfile.with_suffix(".env")
        envfile.write_text(json.dumps(clean_env()))
        tail += [str(envfile), str(child_pidfile)]
    return reaper.popen([sys.executable, "-c", code, *tail], cwd=root, env=env)


@pytest.fixture
def plants(tmp_path, reaper, sleeper_owner):
    """Planted processes of every kind SL-R9 distinguishes."""
    root = tmp_path / "planted"
    root.mkdir()
    base = clean_env()
    live_pid, live_start = sleeper_owner
    dead = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)"])
    dead_pid, dead_start = dead.pid, start_of(dead.pid)
    os.kill(dead.pid, signal.SIGKILL)
    dead.wait()
    assert not live(dead_pid)
    child_pidfile = tmp_path / "child.pid"
    p = {
        "root": root,
        "dead": plant(reaper, root, dict(base, **owner_seam.mark_env(dead_pid, dead_start, root)),
                      child_pidfile=child_pidfile),
        "dead_daemon": plant(reaper, root, dict(base, **owner_seam.mark_env(dead_pid, dead_start, root)),
                             shape="daemon"),
        # the owner's pid is alive, but it is another process than the one that owned it
        "pid_reused": plant(reaper, root, dict(base, **owner_seam.mark_env(live_pid, live_start + 1, root))),
        "live": plant(reaper, root, dict(base, **owner_seam.mark_env(live_pid, live_start, root))),
        "live_daemon": plant(reaper, root, dict(base, **owner_seam.mark_env(live_pid, live_start, root)),
                             shape="daemon"),
        "malformed": plant(reaper, root, dict(base, **owner_seam.mark_env(dead_pid, dead_start, root, malformed=True))),
        "unmarked": plant(reaper, root, dict(base)),
    }
    assert poll(child_pidfile.exists, 10), "the planted parent did not start its child"
    p["child_pid"] = int(child_pidfile.read_text())
    reaper.track(p["child_pid"])
    return p


DIE = ("dead", "dead_daemon", "pid_reused")
SURVIVE = ("live", "live_daemon", "malformed", "unmarked")


def assert_sweep_outcome(p) -> None:
    doomed = [(p[k].pid, start_of(p[k].pid)) if live(p[k].pid) else (p[k].pid, -1) for k in DIE]
    doomed.append((p["child_pid"], start_of(p["child_pid"]) if live(p["child_pid"]) else -1))
    left = wait_gone([(a, b) for a, b in doomed if b != -1], SWEEP_TIMEOUT)
    assert not left, f"marked processes of a dead owner survived the sweep:\n{describe(left)}"
    for kind in SURVIVE:
        assert live(p[kind].pid), f"the sweep killed the {kind} process"


def test_sl_r9_the_sweep_kills_only_marked_processes_of_a_dead_owner_and_their_descendants(plants):
    owner_seam.sweep_dead_owners()
    assert_sweep_outcome(plants)


def test_sl_r9_the_sweep_is_idempotent_and_harmless_when_run_concurrently(plants):
    errors = []

    def sweep():
        try:
            owner_seam.sweep_dead_owners()
        except Exception as exc:                            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sweep) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SWEEP_TIMEOUT + 5)
    owner_seam.sweep_dead_owners()
    assert not errors, errors
    assert_sweep_outcome(plants)


def test_sl_r9_a_new_test_session_runs_the_sweep_at_start(plants):
    env = dict(clean_env(), PYTHONPATH="src")
    for name in list(env):
        if name.startswith(("PYTEST_", "MULTIAGENTS_")):
            del env[name]
    base = BASE / f"sl2-{uuid.uuid4().hex[:12]}"
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", f"--basetemp={base}",
         "tests/test_sl_process_leak.py::test_sl_r3_the_check_ignores_scheduler_processes_that_are_not_the_sessions"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=INNER_SESSION_TIMEOUT)
    assert "1 passed" in res.stdout, res.stdout[-1500:] + res.stderr[-1500:]
    assert_sweep_outcome(plants)


def test_sl_r9_a_sweep_with_nothing_planted_changes_nothing(tmp_path, reaper, sleeper_owner):
    """No marked process at all (and an unmarked scheduler-looking one): a no-op."""
    root = tmp_path / "quiet"
    root.mkdir()
    bystander = plant(reaper, root, dict(clean_env()))
    owner_seam.sweep_dead_owners()
    owner_seam.sweep_dead_owners()
    assert live(bystander.pid) and live(sleeper_owner[0])


def test_sl_r9_the_sweep_kills_a_marked_process_whose_owner_is_a_zombie(tmp_path, reaper, sleeper_owner):
    """The owner is SIGKILLed and left unreaped (a zombie: still in /proc, same
    start time). A zombie owner counts as dead for the sweep too. Selection is by
    the marker, never by argv, so a plain process carrying it dies as well."""
    root = tmp_path / "zombie"
    root.mkdir()
    owner = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)"])
    owner_start = start_of(owner.pid)
    os.kill(owner.pid, signal.SIGKILL)
    assert poll(lambda: (_stat(owner.pid) or ("?",))[0] == "Z", SIGNAL_TIMEOUT), "owner did not become a zombie"
    base = clean_env()
    marked = plant(reaper, root, dict(base, **owner_seam.mark_env(owner.pid, owner_start, root)))
    plain = reaper.popen([sys.executable, "-c", "import time; time.sleep(600)"],
                         env=dict(base, **owner_seam.mark_env(owner.pid, owner_start, root)))
    live_pid, live_start = sleeper_owner
    bystander = plant(reaper, root, dict(base, **owner_seam.mark_env(live_pid, live_start, root)))
    victims = [(marked.pid, start_of(marked.pid)), (plain.pid, start_of(plain.pid))]
    owner_seam.sweep_dead_owners()
    left = wait_gone(victims, SWEEP_TIMEOUT)
    assert not left, f"marked processes of a zombie owner survived the sweep:\n{describe(left)}"
    assert live(bystander.pid), "the sweep killed a process whose owner is alive"
    owner.wait(timeout=SIGNAL_TIMEOUT)          # reap the zombie owner
