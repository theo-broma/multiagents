"""Phase 0, contract B, group G — P0-R8c: the unattended driver compacts its
session at a closed boundary, and only there.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8c.

Driven through `driver._supervise` exactly as `multiagents run --unattended`
drives it: every turn is a real subprocess — a fake provider script installed
in the project's config layer, reached through the real `scripts.exec_action`
— and the compaction, when it happens, is that same script's `compact` action.
The fake logs every invocation (action, argv, cwd, `MULTIAGENTS_*` env); what
the tests assert is that log, the driver's stdout, its exit code, and the
project's events file. Nothing inside the driver is patched except the failure
backoff sleep (thirty real seconds per failed turn), and — for the interactive
path — the detached watcher `_run_supervised` would otherwise fork.

The session's transcript lives where the provider's `transcript:` block says,
under the launch cwd's slug, named by `MULTIAGENTS_SESSION_ID`: the reading the
driver takes (P0-R8a.2) is steered by what each fake turn appends to it.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import p0_context_harness as ch  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents import driver, scripts, watchdog  # noqa: E402
from multiagents.config import AgentSpec, Config  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now  # noqa: E402

SID = "c0ffee00-0000-4000-8000-0000000c0a11"
COMPACT_AT = 8_000
OVER = 9_000
UNDER = COMPACT_AT - 1

LAUNCH, COMPACT = "launch", "compact"


class Loop:
    """A scratch project with a driver node, a fake provider and its transcript."""

    def __init__(self, tmp_path: Path, monkeypatch, *, compact_at: int = COMPACT_AT,
                 timeout: int | None = None, transcript: bool = True,
                 limit_markers: list | None = None):
        self.root = (tmp_path / "proj").resolve()
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.fake = ch.FakeProvider(self.paths.config, tmp_path)
        monkeypatch.setenv("FAKE_LOG", str(self.fake.log))
        monkeypatch.setenv("FAKE_CTL", str(self.fake.ctl))
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        monkeypatch.chdir(self.root)

        self.transcript = tmp_path / "tx" / ch.slug(self.root) / f"{SID}.jsonl"
        data: dict = {"bin": "true", "script": self.fake.name, "spawn": {"args": ["x"]}}
        if transcript:
            block = ch.claude_transcript_block(tmp_path / "tx")
            if limit_markers:
                block["limit_markers"] = limit_markers
            data["transcript"] = block
        limits: dict = {"compact_at_tokens": compact_at}
        if timeout is not None:
            limits["compact_timeout_seconds"] = timeout
        self.provider = Provider.from_dict("fakeprov", data)
        self.config = Config(project={"limits": limits}, providers={"fakeprov": data},
                             agents={}, models={}, instruction_dirs=[])
        self.spec = AgentSpec("orchestrator", "fakeprov", "m")
        self.context = {"MULTIAGENTS_RESUME": "0", "MULTIAGENTS_SESSION_ID": SID,
                        "MULTIAGENTS_ROLE": "orchestrator",
                        "MULTIAGENTS_PROJECT": str(self.root),
                        "MULTIAGENTS_MODEL": "m"}
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        self.tree.add(Node(id="dr-c0ffee", agent="orchestrator", provider="fakeprov",
                           model="m", parent=None, depth=0, status="running",
                           task="orchestrator session", session=SID, session_id=SID,
                           role="orchestrator", started_at=tree_now()))

        # The only patch: a failed turn backs off 30 s, 60 s... in real time.
        fast = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time)
                                        if not n.startswith("__")})
        fast.sleep = lambda seconds: None
        monkeypatch.setattr(driver, "time", fast)

    # ----------------------------------------------------------- setup --

    def reading(self, tokens: int) -> None:
        ch.write_transcript(self.transcript, [ch.user("start"), ch.request(tokens)])

    def node(self, status: str, role: str = "", agent: str = "coder") -> None:
        self.tree.add(Node(id=f"ag-{status[:3]}{len(self.tree.read()['nodes']):03d}",
                           agent=agent, provider="fakeprov", model="m", parent=None,
                           depth=1 if not role else 0, status=status, task="t",
                           role=role, session=SID))

    # ------------------------------------------------------------- run --

    def run(self, turns: list[dict], max_turns: int, compact: dict | None = None) -> int:
        self.fake.control(transcript=str(self.transcript),
                          events=str(self.paths.events_file),
                          turns=turns, compact=compact or {"exit": 0,
                                                            "stdout": "9000 -> 900 tokens\n"})
        return driver._supervise(self.paths, self.config, "orchestrator", self.spec,
                                 self.provider, object(), dict(self.context), max_turns)

    def actions(self) -> list[str]:
        return self.fake.actions()

    def events(self, kind: str) -> list[dict]:
        return ch.events(self.paths.events_file, kind)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    def build(**kwargs) -> Loop:
        return Loop(tmp_path, monkeypatch, **kwargs)
    return build


PRODUCTIVE = {"activity": True}


def productive(tokens: int | None = None, **extra) -> dict:
    turn = {**PRODUCTIVE, **extra}
    if tokens is not None:
        turn["append"] = [ch.user("next"), ch.request(tokens)]
    return turn


# ------------------------------------------------------------ P0-R8c.1 --

def test_p0_r8c_1_all_conditions_hold_compacts(loop):
    lp = loop()
    lp.reading(OVER)
    assert lp.run([productive()], max_turns=1) == 0
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_exactly_at_the_threshold_compacts(loop):
    lp = loop()
    lp.reading(COMPACT_AT)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_every_qualifying_turn_compacts(loop):
    """The fake's compaction does not shrink the transcript, so the reading
    stays over after each turn: each turn meets R8c.1 on its own."""
    lp = loop()
    lp.reading(OVER)
    lp.run([productive()], max_turns=3)
    assert lp.actions() == [LAUNCH, COMPACT] * 3


def test_p0_r8c_1_the_reading_is_taken_after_the_turn(loop):
    """A turn that pushed the session over is the turn that compacts."""
    lp = loop()
    lp.reading(1_000)
    lp.run([productive(OVER)], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_a_turn_that_compacted_itself_below_does_not(loop):
    lp = loop()
    lp.reading(OVER)
    lp.run([{**PRODUCTIVE,
             "append": [ch.compaction(OVER, 700, trigger="auto"), ch.request(1_200)]}],
           max_turns=1)
    assert lp.actions() == [LAUNCH]


def test_p0_r8c_1_under_the_threshold_does_not(loop):
    lp = loop()
    lp.reading(UNDER)
    lp.run([productive()], max_turns=2)
    assert COMPACT not in lp.actions()


def test_p0_r8c_1_compact_at_zero_disables(loop):
    lp = loop(compact_at=0)
    lp.reading(900_000)
    lp.run([productive()], max_turns=2)
    assert COMPACT not in lp.actions()


@pytest.mark.parametrize("why", ["no_file", "no_transcript_block", "no_usage"])
def test_p0_r8c_1_no_reading_never_compacts(loop, why):
    """None is not 0 and not 'over': with no reading nothing is compacted —
    whichever way a None is compared, it must not come out at or above."""
    lp = loop(transcript=(why != "no_transcript_block"))
    if why == "no_usage":
        ch.write_transcript(lp.transcript, [ch.user("hi"), ch.tool_result()])
    elif why == "no_transcript_block":
        lp.reading(OVER)
    lp.run([productive()], max_turns=2)
    assert COMPACT not in lp.actions(), why


@pytest.mark.parametrize("status", ["pending", "running", "stuck"])
def test_p0_r8c_1_cw_r1_a_live_agent_does_not_block_it(loop, status):
    """CW-R1: an active node no longer prevents the compaction."""
    lp = loop()
    lp.reading(OVER)
    lp.node(status)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT], f"not compacted with a {status} agent"


@pytest.mark.parametrize("status", ["done", "merged", "failed", "killed"])
def test_p0_r8c_1_finished_agents_do_not_block_it(loop, status):
    lp = loop()
    lp.reading(OVER)
    lp.node(status)
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT], status


def test_p0_r8c_1_driver_roles_do_not_count_as_live(loop):
    """The orchestrator's own node is running — it is running this loop — and
    an initializer driver node is a driver role too."""
    lp = loop()
    lp.reading(OVER)
    lp.node("running", role="initializer", agent="initializer")
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_cw_r1_a_deferred_task_does_not_block_it(loop):
    lp = loop()
    lp.reading(OVER)
    lp.tree.defer({"agent": "coder", "task": "later"}, tree_now() + 3600, "quota")
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_cw_r1_a_deferred_task_that_is_already_due_does_not_block_it(loop):
    lp = loop()
    lp.reading(OVER)
    lp.tree.defer({"agent": "coder", "task": "now"}, tree_now() - 10, "quota")
    lp.run([productive()], max_turns=1)
    assert lp.actions() == [LAUNCH, COMPACT]


def test_p0_r8c_1_never_after_a_failed_turn(loop):
    lp = loop()
    lp.reading(OVER)
    code = lp.run([{"exit": 1}], max_turns=3)
    assert lp.actions() == [LAUNCH] * 3
    assert code == 1


def test_p0_r8c_1_only_the_successful_turns_of_a_mixed_run(loop):
    lp = loop()
    lp.reading(OVER)
    lp.run([{"exit": 2}, productive(), {"exit": 1}, productive()], max_turns=4)
    assert lp.actions() == [LAUNCH, LAUNCH, COMPACT, LAUNCH, LAUNCH, COMPACT]


def test_p0_r8c_1_never_after_a_limited_turn(loop):
    lp = loop(limit_markers=[{"match": "spend cap reached", "resets": False,
                              "detail": "spend cap"}])
    lp.reading(OVER)
    code = lp.run([{**PRODUCTIVE, "append": [ch.limit_message("You have a spend cap reached.")]}],
                  max_turns=2)
    assert code == 3, "the shipped limit handling stops on a cap that does not reset"
    assert lp.actions() == [LAUNCH]


def test_p0_r8c_1_cw_r1_a_live_agent_finished_by_a_later_turn_changes_nothing(loop, monkeypatch):
    """CW-R1: the agent no longer blocks turn 1's compaction, so it compacts
    after both turns, whether or not the agent finishes during turn 2."""
    lp = loop()
    lp.reading(OVER)
    lp.node("running")
    live = next(n for n in lp.tree.read()["nodes"] if n.startswith("ag-"))

    real_popen = driver.subprocess.Popen
    launches = []

    def popen(*args, **kwargs):
        launches.append(1)
        if len(launches) == 2:
            _finish(lp.tree, live)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(driver.subprocess, "Popen", popen)
    lp.run([productive()], max_turns=2)
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, COMPACT]


def _finish(tree: Tree, node_id: str) -> None:
    with tree.transaction() as data:
        data["nodes"][node_id]["status"] = "done"


def test_p0_r8c_1_the_last_turn_at_the_turn_limit_compacts(loop, capsys):
    """Amendment (order within a turn): the compaction decision precedes the
    turn-limit stop, so the final turn of a run compacts too."""
    lp = loop()
    lp.reading(OVER)
    lp.run([productive()], max_turns=2)
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, COMPACT]
    assert "reached the 2-turn limit" in capsys.readouterr().out


def test_p0_r8c_1_the_headless_fallback_after_a_lost_terminal_compacts(loop, monkeypatch):
    """Amendment: once the terminal is gone the run is unattended, and the
    turns `_run_supervised` then runs headlessly are turns that compact. The
    attached session is simulated as lost (SIGHUP, no tty to retry into);
    everything after that is real."""
    import signal

    lp = loop()
    lp.reading(OVER)
    lp.config.project["limits"]["supervised_turns"] = 1
    lp.fake.control(transcript=str(lp.transcript), events=str(lp.paths.events_file),
                    turns=[productive()], compact={"exit": 0, "stdout": "1 -> 0 tokens\n"})
    monkeypatch.setattr(driver, "_start_supervisor", lambda *a, **k: None)
    monkeypatch.setattr(driver, "_run_attached", lambda *a, **k: -signal.SIGHUP)
    monkeypatch.setattr(driver.sys, "stdin", type("T", (), {"isatty": lambda s: False})())
    driver._run_supervised(lp.paths, lp.config, "orchestrator", lp.spec, lp.provider,
                           object(), dict(lp.context), ["true"], {})
    assert lp.fake.calls(LAUNCH), "the headless fallback never ran a turn"
    assert lp.actions() == [LAUNCH, COMPACT]


# ------------------------------------------------------------ P0-R8c.2 --

def test_p0_r8c_2_the_timeout_key_ships_with_its_default(tmp_path):
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    assert config_mod.load(paths).limits.get("compact_timeout_seconds") == 180


def test_p0_r8c_2_compact_runs_in_the_launch_cwd_with_the_launch_context(loop):
    lp = loop()
    lp.reading(OVER)
    lp.run([productive()], max_turns=1)
    launch = lp.fake.calls(LAUNCH)
    compact = lp.fake.calls(COMPACT)
    assert len(launch) == 1 and len(compact) == 1, lp.actions()
    call = compact[0]
    assert call["argv"][-1] == "compact"
    assert Path(call["cwd"]).resolve() == lp.root
    assert Path(call["cwd"]).resolve() == Path(launch[0]["cwd"]).resolve()
    env = call["env"]
    assert env.get("MULTIAGENTS_SESSION_ID") == SID
    for key in ("MULTIAGENTS_ROLE", "MULTIAGENTS_PROJECT", "MULTIAGENTS_MODEL"):
        assert env.get(key) == lp.context[key], key
    # The script contract's own environment is there too (build_env).
    assert env.get("MULTIAGENTS_PROVIDER") == "fakeprov"


def test_p0_r8c_2_compact_runs_in_the_project_root_whatever_the_process_cwd(
        loop, tmp_path, monkeypatch):
    """Amendment: the compact action's cwd is the project root, whatever
    mechanism carries it there — even when the driver process is elsewhere."""
    lp = loop()
    lp.reading(OVER)
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    lp.run([productive()], max_turns=1)
    compact = lp.fake.calls(COMPACT)
    assert len(compact) == 1, lp.actions()
    assert Path(compact[0]["cwd"]).resolve() == lp.root


def test_p0_r8c_2_compact_is_bounded_by_compact_timeout_seconds(loop):
    lp = loop(timeout=1)
    lp.reading(OVER)
    began = time.monotonic()
    lp.run([productive()], max_turns=1, compact={"sleep": 30, "exit": 0,
                                                  "stdout": "late\n"})
    took = time.monotonic() - began
    assert lp.actions() == [LAUNCH, COMPACT]
    assert took < 15, f"a 1 s compact timeout let a 30 s compaction run {took:.0f} s"


# ------------------------------------------------------------ P0-R8c.3 --

def test_p0_r8c_3_exit_0_prints_the_figures_and_records_compacted(loop, capsys):
    lp = loop()
    lp.reading(OVER)
    lp.run([productive()], max_turns=1,
           compact={"exit": 0, "stdout": "27729 -> 1607 tokens\nsecond line\n"})
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip().startswith("compacted")]
    assert len(lines) == 1, f"expected one 'compacted' line in:\n{out}"
    assert "27729 -> 1607 tokens" in lines[0]
    assert "second line" not in out
    events = lp.events("compacted")
    assert len(events) == 1
    assert events[0].get("tokens_before") == OVER
    assert events[0].get("detail") == "27729 -> 1607 tokens"


def test_p0_r8c_3_exit_64_latches_unsupported_for_the_run(loop):
    lp = loop()
    lp.reading(OVER)
    code = lp.run([productive()], max_turns=3, compact={"exit": 64})
    assert code == 0
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, LAUNCH]
    assert len(lp.events("compact_unsupported")) == 1
    assert lp.events("compacted") == [] and lp.events("compact_failed") == []


def test_p0_r8c_3_the_latch_is_per_driver_run(loop):
    """"for the rest of this driver run": a new `_supervise` asks again."""
    lp = loop()
    lp.reading(OVER)
    lp.run([productive()], max_turns=2, compact={"exit": 64})
    first = lp.actions().count(COMPACT)
    lp.run([productive()], max_turns=1, compact={"exit": 64})
    assert first == 1
    assert lp.actions().count(COMPACT) == 2


def test_p0_r8c_3_other_exit_is_failed_reported_and_the_loop_goes_on(loop, capsys):
    lp = loop()
    lp.reading(OVER)
    code = lp.run([productive()], max_turns=3,
                  compact={"exit": 1, "stderr": "warming up\nsession is locked by pid 42\n"})
    out = capsys.readouterr().out
    assert code == 0
    # Not latched: each qualifying turn tries again.
    assert lp.actions() == [LAUNCH, COMPACT] * 3
    assert "session is locked by pid 42" in out, "the tail of stderr is printed"
    failed = lp.events("compact_failed")
    assert len(failed) == 3
    assert failed[0].get("code") == 1
    assert "detail" in failed[0]
    assert lp.events("compacted") == []


def test_p0_r8c_3_a_failed_compaction_is_not_a_failed_turn(loop):
    """Turn 1 succeeds and its compaction fails; turns 2 and 3 fail. If the
    compaction counted, that is three failures in a row and the run stops at
    turn 3 with exit 1. It does not count: turn 4 runs and the run ends at its
    turn limit."""
    lp = loop()
    lp.reading(OVER)
    code = lp.run([productive(), {"exit": 1}, {"exit": 1}, productive()], max_turns=4,
                  compact={"exit": 1, "stderr": "nope\n"})
    assert lp.fake.calls(LAUNCH).__len__() == 4
    assert code == 0
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, LAUNCH, LAUNCH, COMPACT]


def test_p0_r8c_3_a_timeout_is_a_failure_and_the_next_turn_runs(loop):
    lp = loop(timeout=1)
    lp.reading(OVER)
    code = lp.run([productive()], max_turns=2, compact={"sleep": 30, "exit": 0})
    assert code == 0
    assert lp.actions()[:3] == [LAUNCH, COMPACT, LAUNCH]
    failed = lp.events("compact_failed")
    assert failed, "a timed-out compaction emitted no compact_failed"
    assert failed[0].get("code") not in (0, 64, None)
    assert lp.events("compacted") == []


# ------------------------------------------------------------ P0-R8c.4 --

def test_p0_r8c_4_idle_turns_with_compactions_still_stop(loop, capsys):
    lp = loop()
    lp.reading(OVER)
    code = lp.run([{}], max_turns=10)
    out = capsys.readouterr().out
    assert code == 0
    # Amendment (order within a turn): the second idle turn compacts BEFORE
    # the loop decides to stop.
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, COMPACT]
    assert "nothing left to do" in out


def test_p0_r8c_4_productive_turns_with_compactions_are_not_idle(loop, capsys):
    lp = loop()
    lp.reading(OVER)
    code = lp.run([productive()], max_turns=3)
    assert code == 0
    assert lp.actions() == [LAUNCH, COMPACT] * 3
    assert "reached the 3-turn limit" in capsys.readouterr().out


def test_p0_r8c_4_failed_compactions_do_not_make_idle_turns_productive(loop):
    lp = loop()
    lp.reading(OVER)
    lp.run([{}], max_turns=10, compact={"exit": 1, "stderr": "no\n"})
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, COMPACT]


# ------------------------------------------------------------ P0-R8c.5 --

def test_p0_r8c_5_the_attended_supervised_path_never_compacts(loop, monkeypatch):
    """The attached, interactive part only (amendment): the session ends
    deliberately, so there is no headless fallback."""
    lp = loop()
    lp.reading(OVER)
    lp.fake.control(transcript=str(lp.transcript), events=str(lp.paths.events_file),
                    turns=[productive()], compact={"exit": 0, "stdout": "1 -> 0 tokens\n"})
    monkeypatch.setattr(driver, "_start_supervisor", lambda *a, **k: None)
    argv, env = scripts.exec_action("fakeprov", lp.provider, object(), "launch",
                                    global_config_dir(), lp.paths.config,
                                    extra_env=dict(lp.context))
    driver._run_supervised(lp.paths, lp.config, "orchestrator", lp.spec, lp.provider,
                           object(), dict(lp.context), argv, env)
    assert lp.actions() == [LAUNCH], "an interactive session was compacted"
    assert lp.events("compacted") == [] and lp.events("compact_failed") == []


def test_p0_r8c_5_the_exec_path_watcher_never_compacts(loop, tmp_path):
    """What the exec path leaves running is the detached watcher; it watches
    the CLI until it exits and must never compact under it."""
    import subprocess

    lp = loop()
    lp.reading(OVER)
    lp.fake.control(transcript=str(lp.transcript), compact={"exit": 0})
    spec = AgentSpec.from_dict("orchestrator", {"provider": "fakeprov", "model": "m",
                                                "launch": True, "role": "orchestrator"})
    config = Config(project=lp.config.project, providers=lp.config.providers,
                    agents={"orchestrator": spec}, models={}, instruction_dirs=[])
    child = subprocess.Popen(["sleep", "30"])
    try:
        watchdog.supervise(lp.paths, config, "orchestrator", child.pid,
                           interval=0.2, max_seconds=1.5)
    finally:
        child.kill()
        child.wait()
    assert COMPACT not in lp.actions()
