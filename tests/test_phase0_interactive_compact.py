"""Phase 0, contract B, group G — P0-R8f: interactive compaction by stop,
compact, resume.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8f (amending
P0-R8c.5), with R8a.2 (the reading) and R8c (the headless compaction), as
amended on 2026-09-23 by the tester's read (ag-2c808f) and the advisor's review
(ag-25c350): idle default 300 s, the bell (R8f.8), every value from the config
(R8f.9), and "send ... to cancel" wording.

Two surfaces:

- **The shipped provider scripts** (R8f.1), run as the driver runs them —
  `sh <script> compact` with `MULTIAGENTS_COMPACT_CHECK=1` — with a fake CLI
  first on PATH and `HOME` a scratch directory, as the R8d tests do.
- **`driver._run_supervised`** (R8f.2–R8f.6), the attached interactive path.
  The CLI it holds is a real child process: a fake provider script (Python,
  installed in the project's config layer, reached through the real
  `scripts.exec_action`) whose `launch` stays alive for a set time, appends to
  the session transcript when told, logs a SIGTERM if it gets one, and
  otherwise exits by itself. Its `compact` action answers the probe (check
  mode) and the real call separately, and logs both.

Time: nothing waits 30 or 300 real seconds. The project's limits set
`compact_idle_seconds` and `compact_grace_seconds` to 1 s, and the attached
child's poll interval (`driver.STALL_POLL_SECONDS`) is patched down. A child
that is meant to be stopped lives long enough that a correct driver stops it
well before it would exit; one that must not be stopped lives just past the
point where it would have been. What is asserted is the fake's log (which
actions ran, in what order, with what environment, and whether the child was
terminated), the driver's output and exit code, and the tree's events file.

Besides the poll interval, the only patches are the ones the R8c tests use:
the detached watcher `_run_supervised` would otherwise fork, and stdin, which
is made to look like a terminal so the attached path is the one exercised.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import p0_context_harness as ch  # noqa: E402

from multiagents import driver, scripts, server, watchdog  # noqa: E402
from multiagents.config import AgentSpec, Config  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now  # noqa: E402

SID = "c0ffee00-0000-4000-8000-00000000f8f0"
COMPACT_AT = 8_000
OVER = 9_000
UNDER = COMPACT_AT - 1

IDLE = 1            # limits.compact_idle_seconds in these tests
GRACE = 1           # limits.compact_grace_seconds in these tests
POLL = 0.1          # the attached child's poll interval, patched
STOPPED_BY = 8.0    # a child a correct driver stops lives this long otherwise
KEPT = IDLE + GRACE + 2.0   # a child that must NOT be stopped lives this long

FIGURES = "9000 -> 900 tokens"


# ------------------------------------------------------ the fake provider --

FAKE = r'''#!{python}
"""A provider script whose `launch` is a long-lived CLI. Logs, obeys FAKE_CTL."""
import json, os, pathlib, signal, sys, time

action = sys.argv[1] if len(sys.argv) > 1 else "check"
log = pathlib.Path(os.environ["FAKE_LOG"])
ctl_path = pathlib.Path(os.environ["FAKE_CTL"])
ctl = json.loads(ctl_path.read_text()) if ctl_path.is_file() else {}
env = {k: v for k, v in os.environ.items() if k.startswith("MULTIAGENTS_")}

def record(kind, **extra):
    with log.open("a") as fh:
        fh.write(json.dumps({"action": kind, "t": time.time(), "cwd": os.getcwd(),
                             "env": env, **extra}) + "\n")

def previous(kind):
    if not log.is_file():
        return 0
    return sum(1 for l in log.read_text().splitlines()
               if l.strip() and json.loads(l)["action"] == kind)

def append(records):
    path = pathlib.Path(ctl["transcript"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")

def scheduled():
    events = pathlib.Path(ctl["events"])
    if not events.is_file():
        return False
    return any('"compact_scheduled"' in l for l in events.read_text().splitlines())

if action in ("check", "prepare"):
    sys.exit(0)

if action == "launch":
    n = previous("launch")
    record("launch", n=n + 1)
    launches = ctl.get("launches") or [{}]
    me = launches[min(n, len(launches) - 1)]

    def on_term(*_):
        record("sigterm", n=n + 1)
        sys.exit(143)
    signal.signal(signal.SIGTERM, on_term)

    start = time.time()
    life = float(me.get("life", 0))
    pending = sorted(me.get("appends") or [], key=lambda a: a[0])
    every = me.get("append_every")
    last_every = start
    reacted = False
    while True:
        now = time.time()
        if now - start >= life:
            break
        while pending and now - start >= pending[0][0]:
            append(pending.pop(0)[1]); record("append", n=n + 1)
        if every and now - last_every >= every:
            append([{"type": "user", "sessionId": "s", "message": {
                "role": "user", "content": [{"type": "tool_result",
                                             "tool_use_id": "t", "content": "."}]}}])
            last_every = now
        if me.get("on_scheduled") and not reacted and scheduled():
            reacted = True
            append(me["on_scheduled"]); record("append", n=n + 1, why="scheduled")
            if "then_life" in me:
                life = (time.time() - start) + float(me["then_life"])
        time.sleep(0.02)
    record("exit", n=n + 1)
    sys.exit(int(me.get("exit", 0)))

if action == "compact":
    if os.environ.get("MULTIAGENTS_COMPACT_CHECK") == "1":
        record("probe")
        sys.exit(int(ctl.get("probe", 0)))
    record("compact")
    c = ctl.get("compact") or {}
    if c.get("sleep"):
        time.sleep(float(c["sleep"]))
    sys.stdout.write(c.get("stdout", "")); sys.stdout.flush()
    sys.stderr.write(c.get("stderr", "")); sys.stderr.flush()
    sys.exit(int(c.get("exit", 0)))

sys.exit(64)
'''


class _Tty:
    """stdin as the attached path expects it: a terminal."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("not a real terminal")

    def read(self, *_):
        return ""

    readline = read


class Session:
    """A scratch project, a driver node, the fake provider and its transcript."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys, *,
                 compact_at: int = COMPACT_AT, limits: dict | None = None,
                 limit_markers: list | None = None, transcript_block: bool = True,
                 omit: tuple[str, ...] = ()):
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.root = (tmp_path / "proj").resolve()
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.script = self.paths.config / "providers" / "fakeprov.py"
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text(FAKE.replace("{python}", sys.executable))
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC)
        self.log = tmp_path / "fake.log"
        self.ctl = tmp_path / "fake.ctl.json"
        # Hermetic: nothing of the environment this suite runs in (which may be
        # an agent's own) reaches the fake's logged MULTIAGENTS_* environment.
        # MULTIAGENTS_STATE_DIR stays: it keeps state in the suite's tmp dir.
        for name in [n for n in os.environ
                     if n.startswith("MULTIAGENTS_") and n != "MULTIAGENTS_STATE_DIR"]:
            monkeypatch.delenv(name)
        monkeypatch.setenv("FAKE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_CTL", str(self.ctl))
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        monkeypatch.chdir(self.root)

        self.transcript = tmp_path / "tx" / ch.slug(self.root) / f"{SID}.jsonl"
        data: dict = {"bin": "true", "script": self.script.name,
                      "spawn": {"args": ["x"]}}
        if transcript_block:
            block = ch.claude_transcript_block(tmp_path / "tx")
            if limit_markers:
                block["limit_markers"] = limit_markers
            data["transcript"] = block
        merged = {"compact_at_tokens": compact_at, "compact_idle_seconds": IDLE,
                  "compact_grace_seconds": GRACE, "compact_timeout_seconds": 30,
                  "restart_attempts": 5, "restart_delay_seconds": 0,
                  **(limits or {})}
        for key in omit:        # R8f.9: a project that does not set the key
            merged.pop(key, None)
        self.provider = Provider.from_dict("fakeprov", data)
        self.config = Config(project={"limits": merged}, providers={"fakeprov": data},
                             agents={}, models={}, instruction_dirs=[])
        self.spec = AgentSpec("orchestrator", "fakeprov", "m")
        self.context = {"MULTIAGENTS_RESUME": "0", "MULTIAGENTS_SESSION_ID": SID,
                        "MULTIAGENTS_ROLE": "orchestrator",
                        "MULTIAGENTS_PROJECT": str(self.root),
                        "MULTIAGENTS_MODEL": "m"}
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        self.tree.add(Node(id="dr-f8f000", agent="orchestrator", provider="fakeprov",
                           model="m", parent=None, depth=0, status="running",
                           task="orchestrator session", session=SID, session_id=SID,
                           role="orchestrator", started_at=tree_now()))

        monkeypatch.setattr(driver, "STALL_POLL_SECONDS", POLL)
        monkeypatch.setattr(driver, "_start_supervisor", lambda *a, **k: None)
        monkeypatch.setattr(sys, "stdin", _Tty())

    # ----------------------------------------------------------- setup --

    def reading(self, tokens: int, *, usage: bool = True, aged: float = 30.0) -> None:
        """The session's transcript, at `tokens`, last written `aged` s ago."""
        records = [ch.user("start")]
        records.append(ch.request(tokens) if usage else ch.user("no usage here"))
        ch.write_transcript(self.transcript, records)
        past = time.time() - aged
        os.utime(self.transcript, (past, past))

    def node(self, status: str) -> str:
        agent_id = f"ag-{status[:3]}{len(self.tree.read()['nodes']):03d}"
        self.tree.add(Node(id=agent_id, agent="coder", provider="fakeprov", model="m",
                           parent=None, depth=1, status=status, task="t", session=SID))
        return agent_id

    def finished(self, status: str) -> str:
        """A node that ran and ended with `status`, as every real one does: it
        is created in flight and moves to its final status afterwards."""
        agent_id = self.node("running")
        if status == "merged":
            self.tree.set_status(agent_id, "done")
        self.tree.set_status(agent_id, status)
        return agent_id

    def see(self, agent_id: str, via: str = "check_agent") -> dict:
        """SV-R11: the orchestrator's server returns the node's status to it,
        through one of the three tools that count as seeing a result. The
        server is then discarded, so what it saw survives only if it was
        recorded durably (the spec requires that, as compaction follows)."""
        with self.monkeypatch.context() as m:
            m.setenv("MULTIAGENTS_SESSION_ID", SID)
            m.setenv("MULTIAGENTS_ROLE", "orchestrator")
            # The provider readers are not under test and must not reach the network.
            m.setattr(server.budget_mod, "read_all", lambda *a, **k: {})
            server._reset()
            try:
                if via == "check_agent":
                    got = server.check_agent(agent_id)
                elif via == "collect_agent":
                    got = server.collect_agent(agent_id)
                else:
                    import asyncio
                    got = asyncio.run(server.wait_for_agents([agent_id], 5))
            finally:
                server._reset()
        assert agent_id in json.dumps(got) and "error" not in got, got
        return got

    # ------------------------------------------------------------- run --

    def run(self, launches: list[dict], *, probe: int = 0,
            compact: dict | None = None) -> tuple[int, str]:
        self.ctl.write_text(json.dumps({
            "transcript": str(self.transcript), "events": str(self.paths.events_file),
            "launches": launches, "probe": probe,
            "compact": compact if compact is not None
            else {"exit": 0, "stdout": FIGURES + "\n"}}))
        argv, env = scripts.exec_action("fakeprov", self.provider, object(), "launch",
                                        global_config_dir(), self.paths.config,
                                        extra_env=dict(self.context))
        began = time.monotonic()
        code = driver._run_supervised(self.paths, self.config, "orchestrator",
                                      self.spec, self.provider, object(),
                                      dict(self.context), argv, env)
        self.elapsed = time.monotonic() - began
        out = self.capsys.readouterr()
        return code, out.out + out.err

    # --------------------------------------------------------- observe --

    def calls(self, action: str | None = None) -> list[dict]:
        if not self.log.is_file():
            return []
        out = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [e for e in out if action is None or e["action"] == action]

    def seq(self) -> list[str]:
        """launch / sigterm / probe / compact, in the order they happened."""
        return [e["action"] for e in self.calls()
                if e["action"] in ("launch", "sigterm", "probe", "compact")]

    def stops(self) -> list[dict]:
        return self.calls("sigterm")

    def events(self, kind: str) -> list[dict]:
        return ch.events(self.paths.events_file, kind)


@pytest.fixture
def session(tmp_path, monkeypatch, capsys):
    def build(**kwargs) -> Session:
        return Session(tmp_path, monkeypatch, capsys, **kwargs)
    return build


def stopped_then(*then: dict) -> list[dict]:
    """A first launch a correct driver stops, then the relaunches."""
    return [{"life": STOPPED_BY}, *then] if then else [{"life": STOPPED_BY}, {"life": 0}]


KEEP = [{"life": KEPT}]


def assert_not_stopped(s: Session) -> None:
    assert s.stops() == [], "the session was stopped when R8f.2 does not hold"
    assert s.calls("compact") == [], "a compaction ran when R8f.2 does not hold"
    assert s.events("compact_scheduled") == [], "a compaction was announced"
    assert len(s.calls("launch")) == 1


def assert_stop_compact_resume(s: Session) -> None:
    assert s.seq()[:1] == ["launch"]
    after_probe = [a for a in s.seq() if a != "probe"]
    assert after_probe[:4] == ["launch", "sigterm", "compact", "launch"], (
        f"expected terminate -> compact -> relaunch, saw {s.seq()}")


def announcements(out: str) -> list[str]:
    """The R8f.3 announcement lines in the driver's output. The exact text is
    free; what the contract fixes is that the line gives the token count
    (`9000` or `9,000`) and says to *send* a message to *cancel*. The
    `compacted` line carries the figures but neither word, so it is not one."""
    return [line for line in out.split("\n")
            if "send" in line.lower() and "cancel" in line.lower()
            and re.search(r"(?<![\d,])(9000|9,000)(?![\d,])", line)]


def shows_seconds(line: str, n: int) -> bool:
    """`30s`, `30 s`, `30 sec`, `30 seconds` — but not `130s` or `300s`."""
    return re.search(rf"(?<![\d.]){n}\s*(s\b|sec|second)", line) is not None


# ============================================================== P0-R8f.1 ==
# The probe: `<provider>.sh compact` with MULTIAGENTS_COMPACT_CHECK=1.

FAKE_CLI = r'''#!{python}
"""A stand-in provider CLI. Records that it ran, and appends a manual
compaction record to the session transcript, as the real one would."""
import json, os, pathlib, sys
with open("{log}", "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
argv = sys.argv[1:]
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else ""
slug = os.getcwd().replace("/", "-").replace(".", "-").replace("_", "-")
t = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / slug / f"{sid}.jsonl"
if sid and t.parent.is_dir():
    with t.open("a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                             "compactMetadata": {"trigger": "manual", "preTokens": 9000,
                                                 "postTokens": 900}}) + "\n")
print("{}")
'''


class Probe:
    def __init__(self, tmp_path: Path, name: str):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.cwd = (tmp_path / "work_dir.proj").resolve()
        self.cwd.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "cli.log"
        self.fake = self.bin / name
        self.fake.write_text(FAKE_CLI.replace("{python}", sys.executable)
                             .replace("{log}", str(self.log)))
        self.fake.chmod(0o755)
        self.transcript = (self.home / ".claude" / "projects" / ch.slug(self.cwd)
                           / f"{SID}.jsonl")

    def session(self) -> None:
        ch.write_transcript(self.transcript, [ch.user("hi"), ch.request(OVER)])

    def run(self, script: str, *, sid: str | None = SID, check: str = "1"):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
               "MULTIAGENTS_BIN": str(self.fake), "MULTIAGENTS_MODEL": "m",
               "MULTIAGENTS_PROVIDER": script,
               "MULTIAGENTS_LAUNCH_STATE": str(self.home),
               "MULTIAGENTS_COMPACT_CHECK": check}
        if sid is not None:
            env["MULTIAGENTS_SESSION_ID"] = sid
        return subprocess.run(["sh", str(ch.PROVIDER_SCRIPTS / f"{script}.sh"),
                               "compact"], capture_output=True, text=True,
                              cwd=self.cwd, env=env, timeout=60)

    def cli_ran(self) -> bool:
        return self.log.is_file() and self.log.read_text().strip() != ""


def test_p0_r8f_1_claude_probe_with_a_transcript_says_yes_and_compacts_nothing(tmp_path):
    p = Probe(tmp_path, "claude")
    p.session()
    before = p.transcript.read_bytes()
    result = p.run("claude")
    assert result.returncode == 0, result.stderr
    assert not p.cli_ran(), "the probe started the provider CLI"
    assert p.transcript.read_bytes() == before, "the probe changed the transcript"


def test_p0_r8f_1_claude_probe_without_a_session_id_is_not_now(tmp_path):
    p = Probe(tmp_path, "claude")
    p.session()
    result = p.run("claude", sid=None)
    assert result.returncode not in (0, 64), (
        f"no session id is 'not now', not 'yes' and not 'cannot' "
        f"(got {result.returncode})")
    assert not p.cli_ran()


def test_p0_r8f_1_claude_probe_without_a_transcript_is_not_now(tmp_path):
    p = Probe(tmp_path, "claude")
    result = p.run("claude")
    assert result.returncode not in (0, 64), result.returncode
    assert not p.cli_ran()


def test_p0_r8f_1_claude_probe_for_another_sessions_transcript_is_not_now(tmp_path):
    """"its transcript exists": the one named by the session id, not any."""
    p = Probe(tmp_path, "claude")
    ch.write_transcript(p.transcript.with_name("someone-else.jsonl"),
                        [ch.user("hi"), ch.request(OVER)])
    result = p.run("claude")
    assert result.returncode not in (0, 64), result.returncode
    assert not p.cli_ran()


@pytest.mark.parametrize("script", ["agy", "opencode"])
def test_p0_r8f_1_agy_and_opencode_probe_says_cannot(tmp_path, script):
    p = Probe(tmp_path, script)
    result = p.run(script)
    assert result.returncode == 64, result.returncode
    assert not p.cli_ran()


def test_p0_r8f_1_check_mode_is_only_the_value_1(tmp_path):
    """Anything but `1` is a real compaction, as before R8f: the CLI runs."""
    p = Probe(tmp_path, "claude")
    p.session()
    result = p.run("claude", check="0")
    assert result.returncode == 0, result.stderr
    assert p.cli_ran(), "MULTIAGENTS_COMPACT_CHECK=0 was treated as check mode"


def test_p0_r8f_1_the_readme_documents_check_mode():
    readme = (ch.PROVIDER_SCRIPTS / "README.md").read_text()
    assert "MULTIAGENTS_COMPACT_CHECK" in readme


# ============================================================== P0-R8f.2 ==
# When a compaction is proposed. Each row: the child is or is not stopped.

def test_p0_r8f_2_all_conditions_hold_stops_compacts_and_resumes(session):
    s = session()
    s.reading(OVER)
    code, _ = s.run(stopped_then())
    assert_stop_compact_resume(s)
    assert code == 0
    assert s.elapsed < STOPPED_BY, "the first session ran out its life; nobody stopped it"


def test_p0_r8f_2_1_exactly_at_the_threshold_is_proposed(session):
    s = session()
    s.reading(COMPACT_AT)
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_p0_r8f_2_1_under_the_threshold_is_not(session):
    s = session()
    s.reading(UNDER)
    s.run(KEEP)
    assert_not_stopped(s)


def test_p0_r8f_2_1_compact_at_zero_disables_it(session):
    s = session(compact_at=0)
    s.reading(900_000)
    s.run(KEEP)
    assert_not_stopped(s)


@pytest.mark.parametrize("why", ["no_usage", "no_transcript_block", "no_file"])
def test_p0_r8f_2_1_no_reading_is_never_over(session, why):
    s = session(transcript_block=(why != "no_transcript_block"))
    if why != "no_file":
        s.reading(OVER, usage=(why != "no_usage"))
    s.run(KEEP)
    assert_not_stopped(s)


def test_p0_r8f_2_1_the_reading_is_this_sessions_not_the_newest(session):
    """R8a.2 as amended: another session's transcript over the threshold,
    newer than this one's, does not make this session due."""
    s = session()
    s.reading(1_000)
    other = s.transcript.with_name("another-role.jsonl")
    ch.write_transcript(other, [ch.user("x"), ch.request(OVER)])
    past = time.time() - 20
    os.utime(other, (past, past))
    s.run(KEEP)
    assert_not_stopped(s)


# SV-R11 (context/specs/agent-survival.md) replaced R8f.2.2's idle-tree
# condition with "no unseen result", and CW-R1 (context/specs/cw-compact-while-
# waiting.md) removed that too: running agents, unseen results and deferred
# tasks no longer prevent a compaction. The tests below, which asserted the
# blocking, now assert that the compaction proceeds (changed deliberately, CW-R1).

@pytest.mark.parametrize("status", ["done", "failed"])
def test_p0_r8f_2_2_cw_r1_an_unseen_final_result_does_not_block_it(session, status):
    s = session()
    s.reading(OVER)
    s.finished(status)
    s.run(stopped_then())
    assert_stop_compact_resume(s)


@pytest.mark.parametrize("via", ["check_agent", "wait_for_agents", "collect_agent"])
def test_p0_r8f_2_2_sv_r11_a_result_the_orchestrator_has_seen_does_not(session, via):
    s = session()
    s.reading(OVER)
    agent_id = s.finished("done")
    assert s.see(agent_id, via).get("status", "done") == "done"
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_p0_r8f_2_2_cw_r1_a_result_seen_while_running_does_not_block_either(session):
    s = session()
    s.reading(OVER)
    agent_id = s.node("running")
    s.see(agent_id, "check_agent")
    s.tree.set_status(agent_id, "done")
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_p0_r8f_2_2_cw_r1_an_unseen_result_does_not_block_despite_a_seen_one(session):
    s = session()
    s.reading(OVER)
    s.see(s.finished("done"))
    s.finished("failed")
    s.run(stopped_then())
    assert_stop_compact_resume(s)


@pytest.mark.parametrize("retry_in", [3600, -10])
def test_p0_r8f_2_2_cw_r1_a_deferred_task_does_not_block_it_due_or_not(session, retry_in):
    s = session()
    s.reading(OVER)
    s.tree.defer({"agent": "coder", "task": "later"}, tree_now() + retry_in, "quota")
    s.run(stopped_then())
    assert_stop_compact_resume(s)


@pytest.mark.parametrize("status", ["done", "failed", "merged", "interrupted"])
def test_p0_r8f_2_2_finished_agents_do_not_block_it(session, status):
    """Finished, and (SV-R11) already returned to the orchestrator."""
    s = session()
    s.reading(OVER)
    s.see(s.finished(status))
    s.run(stopped_then())
    assert_stop_compact_resume(s)


def test_p0_r8f_2_3_a_transcript_that_keeps_changing_is_not_at_rest(session):
    """The model is working: something is appended more often than the idle
    window. However long it goes on, the session is not stopped."""
    s = session()
    s.reading(OVER)
    s.run([{"life": KEPT + 1, "append_every": IDLE / 3}])
    assert_not_stopped(s)


def test_p0_r8f_2_3_rest_is_measured_from_the_last_change(session):
    """A change at 0.5 s restarts the idle clock: the stop comes no earlier
    than idle + grace after it."""
    s = session()
    s.reading(OVER)
    s.run([{"life": STOPPED_BY, "appends": [[0.5, [ch.user("more")]]]}, {"life": 0}])
    assert_stop_compact_resume(s)
    last = max(e["t"] for e in s.calls("append"))
    stop = s.stops()[0]["t"]
    assert stop - last >= IDLE + GRACE - 0.05, (
        f"stopped {stop - last:.2f}s after the last change; "
        f"idle {IDLE}s + grace {GRACE}s had not passed")


def test_p0_r8f_2_4_a_pending_usage_limit_wins(session):
    """The limit path and the compaction path must not both act: with the
    CLI's limit message the last thing said, nothing is announced or compacted.
    A cap that does not reset makes the limit path end the run (exit 3)."""
    s = session(limit_markers=[{"match": "spend cap reached", "resets": False,
                                "detail": "spend cap"}])
    ch.write_transcript(s.transcript, [ch.user("go"), ch.request(OVER),
                                       ch.limit_message("You have a spend cap reached.")])
    past = time.time() - 30
    os.utime(s.transcript, (past, past))
    code, _ = s.run([{"life": STOPPED_BY}])
    assert code == 3
    assert s.calls("compact") == []
    assert s.events("compact_scheduled") == []
    assert s.events("compacted") == []


def test_p0_r8f_2_5_the_probe_is_asked_before_anything_is_stopped(session):
    s = session()
    s.reading(OVER)
    s.run(stopped_then())
    probes = s.calls("probe")
    assert probes, "no probe (compact with MULTIAGENTS_COMPACT_CHECK=1) was run"
    first_stop = s.stops()[0]["t"]
    assert probes[0]["t"] < first_stop
    assert probes[0]["env"].get("MULTIAGENTS_SESSION_ID") == SID
    assert Path(probes[0]["cwd"]).resolve() == s.root
    scheduled = s.events("compact_scheduled")
    assert scheduled and probes[0]["t"] <= scheduled[0]["t"], (
        "announced before the probe said yes")


@pytest.mark.parametrize("answer", [64, 1, 2])
def test_p0_r8f_2_5_a_probe_that_does_not_say_yes_stops_nothing(session, answer):
    s = session()
    s.reading(OVER)
    s.run(KEEP, probe=answer)
    assert_not_stopped(s)


def test_p0_r8f_2_6_disabled_after_a_failure_for_the_rest_of_the_run(session):
    """R8f.5's latch, seen from R8f.2: the relaunched session is over, idle and
    at rest, and is left alone."""
    s = session()
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}),
          compact={"exit": 1, "stderr": "API Error: 529 overloaded\n"})
    assert len(s.stops()) == 1
    assert len(s.calls("compact")) == 1


# ============================================================== P0-R8f.3 ==

def test_p0_r8f_3_the_announcement_line_and_event(session):
    """As amended by the advisor's review: one line that gives the tokens and
    the configured grace seconds, and says a *sent* message cancels."""
    s = session()
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1, (
        f"expected one announcement line with the tokens, 'send' and 'cancel':\n{out}")
    assert shows_seconds(lines[0], GRACE), (
        f"the announcement does not show the grace period ({GRACE}s): {lines[0]!r}")
    assert "type anything" not in out.lower(), (
        "the withdrawn 'type anything to keep it' wording is still printed")
    scheduled = s.events("compact_scheduled")
    assert len(scheduled) == 1
    assert scheduled[0].get("tokens") == OVER


def test_p0_r8f_3_the_stop_comes_no_earlier_than_the_grace_period(session):
    s = session(limits={"compact_grace_seconds": 2})
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert lines and shows_seconds(lines[0], 2), (
        f"the announcement does not show the configured 2s:\n{out}")
    scheduled = s.events("compact_scheduled")
    assert scheduled and s.stops()
    waited = s.stops()[0]["t"] - scheduled[0]["t"]
    assert waited >= 2 - 0.05, f"stopped {waited:.2f}s after the announcement"


def test_p0_r8f_3_a_change_during_the_grace_period_cancels(session):
    """The user submits something after the announcement: no stop, one
    `compact_cancelled`. The session ends by itself before it could come
    back to rest, so there is no second proposal to confuse the count."""
    s = session()
    s.reading(OVER)
    code, _ = s.run([{"life": STOPPED_BY, "on_scheduled": [ch.user("keep it")],
                      "then_life": IDLE / 2}])
    assert s.stops() == [], "the session was stopped after the user answered"
    assert s.calls("compact") == []
    assert len(s.events("compact_scheduled")) == 1
    assert len(s.events("compact_cancelled")) == 1
    assert code == 0


def test_p0_r8f_3_after_a_cancel_it_is_proposed_again_once_back_at_rest(session):
    s = session()
    s.reading(OVER)
    s.run([{"life": STOPPED_BY + 4, "on_scheduled": [ch.user("keep it")]},
           {"life": 0}])
    assert len(s.events("compact_cancelled")) == 1
    assert len(s.events("compact_scheduled")) == 2
    assert len(s.stops()) == 1
    reacted = [e for e in s.calls("append") if e.get("why") == "scheduled"][0]["t"]
    assert s.stops()[0]["t"] - reacted >= IDLE + GRACE - 0.05, (
        "proposed again before the transcript had come back to rest")


def test_p0_r8f_3_the_limit_keys_ship_with_their_defaults():
    limits = yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]
    assert limits.get("compact_idle_seconds") == 300
    assert limits.get("compact_grace_seconds") == 30


def test_p0_r8f_3_the_defaults_reach_a_project_that_does_not_set_them(tmp_path):
    from multiagents import config as config_mod

    paths = ProjectPaths(tmp_path)
    paths.ensure()
    limits = config_mod.load(paths).limits
    assert limits.get("compact_idle_seconds") == 300
    assert limits.get("compact_grace_seconds") == 30
    assert limits.get("compact_bell") is True


# ============================================================== P0-R8f.4 ==

def test_p0_r8f_4_terminate_then_compact_then_relaunch(session):
    s = session()
    s.reading(OVER)
    s.run(stopped_then())
    assert_stop_compact_resume(s)
    stop, compact = s.stops()[0], s.calls("compact")[0]
    relaunch = s.calls("launch")[1]
    assert stop["t"] <= compact["t"] <= relaunch["t"]


def test_p0_r8f_4_the_compact_call_is_the_r8c_2_call(session):
    s = session()
    s.reading(OVER)
    s.run(stopped_then())
    compact = s.calls("compact")
    assert len(compact) == 1
    assert compact[0]["env"].get("MULTIAGENTS_SESSION_ID") == SID
    assert compact[0]["env"].get("MULTIAGENTS_COMPACT_CHECK", "") != "1", (
        "the real compaction ran in check mode")
    assert Path(compact[0]["cwd"]).resolve() == s.root


def test_p0_r8f_4_the_relaunch_resumes_the_same_session_with_no_prompt(session):
    s = session()
    s.reading(OVER)
    s.run(stopped_then())
    launches = s.calls("launch")
    assert len(launches) == 2
    env = launches[1]["env"]
    assert env.get("MULTIAGENTS_RESUME") == "1"
    assert env.get("MULTIAGENTS_SESSION_ID") == SID
    assert not env.get("MULTIAGENTS_RESUME_PROMPT"), (
        "the orchestrator was idle; nothing may be sent to the model on resume")
    assert env.get("MULTIAGENTS_COMPACT_CHECK", "") != "1"


def test_p0_r8f_4_prints_compacted_and_emits_the_event(session):
    s = session()
    s.reading(OVER)
    _, out = s.run(stopped_then())
    assert re.search(rf"compacted\s+{re.escape(FIGURES)}", out), out
    done = s.events("compacted")
    assert len(done) == 1
    assert done[0].get("tokens_before") == OVER
    assert done[0].get("detail") == FIGURES


def test_p0_r8f_4_the_driver_keeps_running_and_ends_with_the_session(session):
    """The relaunched session is the session: when the person ends it with a
    clean exit, the run ends as any clean exit does."""
    s = session()
    s.reading(OVER)
    code, out = s.run(stopped_then({"life": 0.5, "exit": 0}))
    assert code == 0
    assert "Carrying on headlessly" not in out
    assert "Retrying in" not in out


def test_p0_r8f_4_does_not_consume_restart_attempts(session):
    """With one restart attempt configured, a terminal loss after the
    compaction still gets its one retry — the compaction did not spend it."""
    s = session(limits={"restart_attempts": 1})
    s.reading(OVER)
    code, out = s.run(stopped_then({"life": 0.3, "exit": 129}, {"life": 0}))
    assert s.seq().count("compact") == 1
    launches = s.calls("launch")
    assert len(launches) == 3, f"expected a retry after the terminal loss: {s.seq()}"
    assert launches[2]["env"].get("MULTIAGENTS_RESUME_PROMPT"), (
        "the third launch is the restart path's, which carries the resume prompt")
    assert "(1/1)" in out
    assert code == 0


# ============================================================== P0-R8f.5 ==

def test_p0_r8f_5_a_failed_compaction_still_relaunches_and_disables(session):
    s = session()
    s.reading(OVER)
    code, out = s.run(stopped_then({"life": KEPT}),
                      compact={"exit": 1, "stderr": "API Error: 529 overloaded\n"})
    launches = s.calls("launch")
    assert len(launches) == 2, "the session was lost after a failed compaction"
    assert launches[1]["env"].get("MULTIAGENTS_RESUME") == "1"
    assert launches[1]["env"].get("MULTIAGENTS_SESSION_ID") == SID
    failed = s.events("compact_failed")
    assert len(failed) == 1 and failed[0].get("code") == 1
    assert "529 overloaded" in out, "the failure line does not carry the reason"
    assert s.events("compacted") == []
    assert len(s.stops()) == 1, "stopped again in the same run after a failure"
    assert code == 0


def test_p0_r8f_5_a_compaction_that_times_out_still_relaunches(session):
    s = session(limits={"compact_timeout_seconds": 1})
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}), compact={"exit": 0, "sleep": 4,
                                                 "stdout": FIGURES + "\n"})
    assert len(s.calls("launch")) == 2
    assert s.calls("launch")[1]["env"].get("MULTIAGENTS_RESUME") == "1"
    assert len(s.events("compact_failed")) == 1
    assert s.events("compacted") == []
    assert len(s.stops()) == 1


def test_p0_r8f_5_unsupported_after_a_yes_relaunches_and_disables(session):
    """The probe said yes and the real call said 64: `compact_unsupported`,
    a relaunch, and no second stop."""
    s = session()
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}), compact={"exit": 64})
    assert len(s.calls("launch")) == 2
    assert s.calls("launch")[1]["env"].get("MULTIAGENTS_RESUME") == "1"
    assert len(s.events("compact_unsupported")) == 1
    assert s.events("compact_failed") == []
    assert len(s.stops()) == 1


def test_p0_r8f_5_once_per_crossing_after_a_success(session):
    """The fake's compaction does not shrink the transcript, so the relaunched
    session is still over, idle and at rest. It is not stopped again."""
    s = session()
    s.reading(OVER)
    s.run(stopped_then({"life": KEPT}))
    assert len(s.stops()) == 1
    assert len(s.calls("compact")) == 1
    assert len(s.events("compact_scheduled")) == 1


def test_p0_r8f_5_a_new_crossing_is_proposed_again(session):
    """After the compaction the reading falls below the threshold, then the
    session grows past it again: a second stop, compact and resume."""
    s = session()
    s.reading(OVER)
    s.run(stopped_then(
        {"life": STOPPED_BY + 6,
         "appends": [[0.1, [ch.compaction(OVER, 900), ch.request(1_000)]],
                     [IDLE + 2.0, [ch.user("more"), ch.request(OVER)]]]},
        {"life": 0}))
    assert len(s.stops()) == 2, f"no second stop after a new crossing: {s.seq()}"
    assert len(s.calls("compact")) == 2
    assert len(s.calls("launch")) == 3


# ============================================================== P0-R8f.6 ==

def test_p0_r8f_6_the_exec_path_watcher_never_probes(session):
    """What the exec handover leaves running is the detached watcher."""
    s = session()
    s.reading(OVER)
    s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
                                 "events": str(s.paths.events_file),
                                 "compact": {"exit": 0}}))
    spec = AgentSpec.from_dict("orchestrator", {"provider": "fakeprov", "model": "m",
                                                "launch": True, "role": "orchestrator"})
    config = Config(project=s.config.project, providers=s.config.providers,
                    agents={"orchestrator": spec}, models={}, instruction_dirs=[])
    child = subprocess.Popen(["sleep", "30"])
    try:
        watchdog.supervise(s.paths, config, "orchestrator", child.pid,
                           interval=0.2, max_seconds=IDLE + GRACE + 1.5)
    finally:
        child.kill()
        child.wait()
    assert s.calls("probe") == []
    assert s.calls("compact") == []
    assert s.events("compact_scheduled") == []


def test_p0_r8f_6_the_exec_handover_never_probes(session, monkeypatch):
    """`_launch_agent(supervise=False)` up to the handover: no probe."""
    s = session()
    s.reading(OVER)
    s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
                                 "events": str(s.paths.events_file),
                                 "compact": {"exit": 0}}))
    handed: list = []
    monkeypatch.setattr(driver, "_hand_over", lambda argv, env, script:
                        handed.append(argv) or 0)
    spec = AgentSpec.from_dict("orchestrator", {"provider": "fakeprov", "model": "m",
                                                "launch": True, "role": "orchestrator"})
    config = Config(project=s.config.project, providers=s.config.providers,
                    agents={"orchestrator": spec}, models={}, instruction_dirs=[])
    monkeypatch.setattr(driver, "_auth_problem", lambda *a, **k: "")
    monkeypatch.setattr(driver, "_other_driver_running", lambda *a, **k: None)
    driver._launch_agent(s.paths, config, "orchestrator", resume=True, supervise=False)
    assert handed, "the exec path did not reach the handover"
    assert s.calls("probe") == []
    assert s.calls("compact") == []


def test_p0_r8f_6_the_headless_loop_uses_neither_probe_nor_grace(session):
    """`_supervise` keeps R8c: compact straight after a qualifying turn."""
    s = session()
    s.reading(OVER)
    s.ctl.write_text(json.dumps({"transcript": str(s.transcript),
                                 "events": str(s.paths.events_file),
                                 "launches": [{"life": 0}],
                                 "compact": {"exit": 0, "stdout": FIGURES + "\n"}}))
    driver._supervise(s.paths, s.config, "orchestrator", s.spec, s.provider, object(),
                      dict(s.context), 1)
    assert s.calls("probe") == []
    assert s.events("compact_scheduled") == []
    assert s.seq() == ["launch", "compact"]


# ============================================================== P0-R8f.7 ==

def _section() -> str:
    from multiagents import config as config_mod

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = ProjectPaths(Path(tmp))
        paths.ensure()
        config = config_mod.load(paths)
        spec = driver._launched_spec(config, "orchestrator", "implement")
        assert spec is not None
        text = config.instructions_for(spec)
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.strip() == "## Your own context window"]
    assert starts, "no '## Your own context window' heading in the composed prompt"
    body = []
    for line in lines[starts[0] + 1:]:
        if re.match(r"#{1,2} ", line):
            break
        body.append(line)
    return "\n".join(body)


def _paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()).lower() for p in re.split(r"\n\s*\n", text) if p.strip()]


def test_p0_r8f_7_the_orchestrator_is_told_run_may_stop_and_resume_it():
    hits = [p for p in _paragraphs(_section())
            if "multiagents run" in p and "resum" in p and "stop" in p
            and "announc" in p]
    assert hits, ("no paragraph says that under `multiagents run` the driver may "
                  "stop and resume the session after announcing it")
    assert any("on disk" in p for p in hits), (
        "it does not tell the orchestrator to end a boundary turn with its state on disk")


def test_p0_r8f_7_compact_is_asked_for_only_where_the_driver_cannot():
    """`/compact` is still named, but only for the exec path or a provider
    that cannot compact — not unconditionally for every interactive session."""
    paras = [p for p in _paragraphs(_section()) if "/compact" in p]
    assert paras, "the /compact advice is gone entirely"
    for p in paras:
        assert "exec" in p or "cannot compact" in p or "can't compact" in p, (
            f"/compact is still advised unconditionally: {p!r}")


def test_p0_r8f_7_only_a_sent_message_cancels():
    """Advisor's amendment: the brief also says that only a *sent* message
    cancels an announced compaction — typing without sending does not."""
    hits = [p for p in _paragraphs(_section())
            if "cancel" in p and ("sent" in p or "send" in p)]
    assert hits, ("no paragraph of the brief says that only a sent message "
                  "cancels the compaction")


def test_p0_r8f_7_typed_but_unsubmitted_text_is_invisible():
    """R8f.3: text typed but not yet submitted cannot be seen; the brief says so."""
    section = " ".join(_section().split()).lower()
    assert "typed" in section and "submit" in section


# ============================================================== P0-R8f.8 ==
# The announcement rings: a `\a` on the announcement line when
# `limits.compact_bell` is true (the default), none at all when false.

def test_p0_r8f_8_the_announcement_rings_by_default(session):
    """The project does not set `compact_bell`: the default, true, applies."""
    s = session()
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1, f"no announcement line in:\n{out!r}"
    assert "\a" in lines[0], f"the announcement does not ring: {lines[0]!r}"


def test_p0_r8f_8_the_announcement_rings_when_the_bell_is_on(session):
    s = session(limits={"compact_bell": True})
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert len(lines) == 1, f"no announcement line in:\n{out!r}"
    assert "\a" in lines[0], f"the announcement does not ring: {lines[0]!r}"


def test_p0_r8f_8_no_bell_at_all_when_it_is_off(session):
    s = session(limits={"compact_bell": False})
    s.reading(OVER)
    _, out = s.run(stopped_then())
    assert len(announcements(out)) == 1, f"no announcement line in:\n{out!r}"
    assert "\a" not in out, "a bell was written with compact_bell: false"
    assert_stop_compact_resume(s)


def test_p0_r8f_8_the_bell_ships_on():
    limits = yaml.safe_load((ch.SHIPPED / "project.yaml").read_text())["limits"]
    assert limits.get("compact_bell") is True


# ============================================================== P0-R8f.9 ==
# Every R8f value comes from the project's `limits`: an override takes
# effect, an omission gets the shipped default, a malformed value falls back
# to the default and never crashes the driver. Only observable effects are
# asserted: the proposal time, the grace delay and the seconds shown, the bell.
#
# The idle default (300 s) is too long to wait for, so for that key the
# default is seen from one side only: a transcript at rest for 120 s — past a
# 60 s default, past a zero or negative value taken literally — is not stopped.

AT_REST_UNDER_DEFAULT = 120.0


def test_p0_r8f_9_an_overridden_idle_period_sets_the_proposal_time(session):
    s = session(limits={"compact_idle_seconds": 2})
    s.reading(OVER)
    s.run([{"life": STOPPED_BY, "appends": [[0.3, [ch.user("more")]]]}, {"life": 0}])
    assert s.stops(), "never stopped: the configured idle period (2s) was not used"
    assert_stop_compact_resume(s)
    last = max(e["t"] for e in s.calls("append"))
    waited = s.stops()[0]["t"] - last
    assert waited >= 2 + GRACE - 0.05, (
        f"stopped {waited:.2f}s after the last change; the configured idle 2s "
        f"+ grace {GRACE}s had not passed")


def test_p0_r8f_9_an_overridden_grace_period_is_waited_and_shown(session):
    s = session(limits={"compact_grace_seconds": 3})
    s.reading(OVER)
    _, out = s.run([{"life": STOPPED_BY + 3}, {"life": 0}])
    lines = announcements(out)
    assert lines and shows_seconds(lines[0], 3), (
        f"the announcement does not show the configured 3s:\n{out}")
    scheduled = s.events("compact_scheduled")
    assert scheduled and s.stops(), "never stopped with a 3s grace period"
    waited = s.stops()[0]["t"] - scheduled[0]["t"]
    assert waited >= 3 - 0.05, f"stopped {waited:.2f}s after the announcement"


def test_p0_r8f_9_an_omitted_idle_period_is_the_default_not_zero(session):
    s = session(omit=("compact_idle_seconds",))
    s.reading(OVER, aged=AT_REST_UNDER_DEFAULT)
    code, _ = s.run(KEEP)
    assert code == 0
    assert_not_stopped(s)


def test_p0_r8f_9_an_omitted_grace_period_is_the_default(session):
    """Grace 30 s: announced, showing 30, and not stopped within seconds."""
    s = session(omit=("compact_grace_seconds",))
    s.reading(OVER)
    code, out = s.run(KEEP)
    assert code == 0
    lines = announcements(out)
    assert lines and shows_seconds(lines[0], 30), (
        f"the announcement does not show the default 30s:\n{out}")
    assert s.stops() == [], "stopped long before the default 30s grace period"


def test_p0_r8f_9_an_omitted_bell_is_the_default_on(session):
    s = session(omit=("compact_bell",))
    s.reading(OVER)
    _, out = s.run(stopped_then())
    lines = announcements(out)
    assert lines and "\a" in lines[0], f"no bell on the announcement: {out!r}"


@pytest.mark.parametrize("value", ["5m", -1, float("inf"), "inf"],
                         ids=["not_a_number", "negative", "inf", "inf_string"])
def test_p0_r8f_9_a_malformed_idle_period_falls_back_to_the_default(session, value):
    s = session(limits={"compact_idle_seconds": value})
    s.reading(OVER, aged=AT_REST_UNDER_DEFAULT)
    code, _ = s.run(KEEP)
    assert code == 0, "a malformed compact_idle_seconds ended the run"
    assert_not_stopped(s)


@pytest.mark.parametrize("value", ["soon", -5, float("inf"), "inf"],
                         ids=["not_a_number", "negative", "inf", "inf_string"])
def test_p0_r8f_9_a_malformed_grace_period_falls_back_to_the_default(session, value):
    s = session(limits={"compact_grace_seconds": value})
    s.reading(OVER)
    code, out = s.run(KEEP)
    assert code == 0, "a malformed compact_grace_seconds ended the run"
    lines = announcements(out)
    assert lines and shows_seconds(lines[0], 30), (
        f"the announcement does not show the default 30s:\n{out}")
    assert s.stops() == [], "stopped long before the default 30s grace period"


@pytest.mark.parametrize("value", ["false", "yes", 0, None],
                         ids=["string_false", "string_yes", "zero", "empty"])
def test_p0_r8f_9_a_malformed_bell_falls_back_to_the_default_on(session, value):
    s = session(limits={"compact_bell": value})
    s.reading(OVER)
    code, out = s.run(stopped_then())
    assert code == 0, "a malformed compact_bell ended the run"
    lines = announcements(out)
    assert lines and "\a" in lines[0], (
        f"a non-boolean compact_bell did not fall back to the bell: {out!r}")
