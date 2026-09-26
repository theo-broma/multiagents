"""The launch wrapper every agent runs under (SV-R1, SV-R2, SV-R4).

    python agentwrap.py RUN_DIR DEADLINE PID_FILE -- ARGV...

It exists so that an agent does not depend on the server that started it. The
server used to hold the agent's stdout and stderr as pipes, which made the
agent's life as long as the server's: when the orchestrator's CLI went away
the pipe closed, and the agent either died of SIGPIPE or wrote into nothing.
Here the agent's output goes to files the next server can read:

* stdout is APPENDED to ``RUN_DIR/output.ndjson`` — a steer's turn follows the
  last one in the same file, and the server records where each turn starts;
* stderr goes to ``RUN_DIR/stderr.log``, one turn at a time;
* when the agent ends, its exit code is written to ``RUN_DIR/exit_status``,
  or ``timeout`` when this wrapper ended it at DEADLINE (an absolute epoch
  time, 0 for none). Written by rename, so a reader never sees half of it.

The deadline is the bound on an agent NOBODY is watching (SV-R4). While a
server holds the node's supervision lock (``RUN_DIR/supervisor.lock``, SV-R5)
the timeout is that server's watchdog's to report, as it always was, and this
wrapper leaves it running; the moment no server holds it — gone, or never
adopted it — an agent past its deadline is ended.

The agent gets a process group of its own, and its pid (= that group) goes to
PID_FILE, so something that finds this wrapper dead can still reach the agent.
This wrapper stays out of that group: it has to outlive the kill to record it.
TERM, HUP and INT to the wrapper are forwarded to the agent's group, escalated
to KILL after ``GRACE`` seconds, so stopping the wrapper stops the agent.

Standard library only, and no import from the package: in a container it runs
under whatever ``python3`` the image has, passed as ``python3 -c <source>``
rather than by a path the container may not have mounted.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time

GRACE = 2.0          # TERM, then KILL this long after
POLL = 0.1

OUTPUT = "output.ndjson"
STDERR = "stderr.log"
EXIT_STATUS = "exit_status"
WRAPPER_PID = "wrapper.pid"
SUPERVISOR_LOCK = "supervisor.lock"
TIMEOUT = "timeout"
LOCK_PROBE = 1.0     # how often, past the deadline, to ask whether anyone watches


def _write_atomic(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def _exited(pid):
    """Has `pid` exited? Without reaping it, so its group id stays reserved
    until the stragglers in it have been killed."""
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return info is not None and info.si_pid == pid


def _supervised(run_dir):
    """Does a server hold this run's supervision lock right now?"""
    try:
        fd = os.open(os.path.join(run_dir, SUPERVISOR_LOCK), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)          # closing drops the probe's own shared lock
    return False


def main(argv):
    if len(argv) < 5 or argv[3] != "--":
        sys.stderr.write("usage: agentwrap RUN_DIR DEADLINE PID_FILE -- ARGV...\n")
        return 2
    run_dir, deadline, pid_file, command = argv[0], float(argv[1]), argv[2], argv[4:]
    status_path = os.path.join(run_dir, EXIT_STATUS)
    try:
        os.unlink(status_path)
    except FileNotFoundError:
        pass
    _write_atomic(os.path.join(run_dir, WRAPPER_PID), "%d\n" % os.getpid())

    out = open(os.path.join(run_dir, OUTPUT), "ab")
    err = open(os.path.join(run_dir, STDERR), "wb")
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, preexec_fn=os.setpgrp)
    except OSError as exc:
        err.write(("agentwrap: could not start %s: %s\n" % (command[0], exc)).encode())
        err.close()
        _write_atomic(status_path, "127\n")
        return 0
    out.close()
    err.close()
    _write_atomic(pid_file, "%d\n" % proc.pid)

    state = {"kill_at": None, "timed_out": False, "probed": 0.0}

    def end(timed_out=False):
        if state["kill_at"] is None:
            state["kill_at"] = time.monotonic() + GRACE
            _killpg(proc.pid, signal.SIGTERM)
        if timed_out:
            state["timed_out"] = True

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: end())

    while not _exited(proc.pid):
        if (deadline and time.time() >= deadline and not state["timed_out"]
                and time.monotonic() - state["probed"] >= LOCK_PROBE):
            state["probed"] = time.monotonic()
            if not _supervised(run_dir):
                end(timed_out=True)
        if state["kill_at"] is not None and time.monotonic() >= state["kill_at"]:
            _killpg(proc.pid, signal.SIGKILL)
        time.sleep(POLL)

    # Whatever the agent left behind in its group — a heartbeat, a dev server —
    # goes with it: nothing is reading their output any more.
    _killpg(proc.pid, signal.SIGKILL)
    code = proc.wait()
    _write_atomic(status_path, (TIMEOUT if state["timed_out"] else str(code)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
