"""Attack on P0-R8c — `driver._supervise` and its compaction at a closed boundary.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8c.1–R8c.3.
Driven exactly as `test_phase0_unattended_compact.py` drives it (its `Loop`
fixture is reused): real subprocess turns through a fake provider script. The
fake's `compact` arm is extended here with what a real CLI can do and the
contract's fake cannot: write bytes that are not UTF-8, write one enormous
stderr line, and leave a child process behind when the script is killed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_context_harness as ch  # noqa: E402
from test_phase0_unattended_compact import (  # noqa: E402
    COMPACT, LAUNCH, OVER, Loop, productive)

EXTRA_COMPACT = r'''
if action == "compact":
    c = ctl.get("compact") or {}
    if c.get("stderr_hex"):
        sys.stderr.buffer.write(bytes.fromhex(c["stderr_hex"])); sys.stderr.flush()
    if c.get("stdout_hex"):
        sys.stdout.buffer.write(bytes.fromhex(c["stdout_hex"])); sys.stdout.flush()
    if c.get("stderr_bytes"):
        sys.stderr.write("x" * int(c["stderr_bytes"])); sys.stderr.flush()
    if c.get("orphan"):
        # What `claude.sh compact` is: a shell whose child is the CLI doing
        # the work. Killing the shell does not kill its child.
        import subprocess
        subprocess.Popen([sys.executable, "-c",
            "import time, pathlib; time.sleep(%f); "
            "pathlib.Path(%r).write_text('the compaction ran on')"
            % (float(c["orphan_after"]), c["orphan"])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=False)
'''


@pytest.fixture
def loop(tmp_path, monkeypatch):
    def build(**kwargs) -> Loop:
        lp = Loop(tmp_path, monkeypatch, **kwargs)
        text = lp.fake.script.read_text()
        marker = 'if action == "compact":'
        lp.fake.script.write_text(text.replace(marker, EXTRA_COMPACT.strip() + "\n"
                                               + marker, 1))
        return lp
    return build


def _survives(lp, **kwargs) -> int:
    try:
        return lp.run(**kwargs)
    except Exception as exc:  # noqa: BLE001 — raising at all is the defect
        pytest.fail(f"the unattended driver crashed: {type(exc).__name__}: {exc}")


# -------------------------------------------------- R8c.3, what comes back --

@pytest.mark.parametrize("stream", ["stderr_hex", "stdout_hex"])
def test_attack_r8c_3_non_utf8_output_from_compact_does_not_crash_the_driver(
        loop, stream):
    """`scripts.run_action` captures with `text=True` and catches only
    TimeoutExpired and OSError; a script that writes one byte that is not
    UTF-8 (a CLI error echoing a binary path, a locale-encoded message) makes
    `subprocess.run` raise UnicodeDecodeError straight out of `_supervise`.
    The unattended run dies after a successful turn. R8c.3: anything but 0/64
    is a failed compaction and the loop continues."""
    lp = loop()
    lp.reading(OVER)
    code = _survives(lp, turns=[productive()], max_turns=2,
                     compact={"exit": 1, stream: "ff fe 62 6f 6f 6d 0a"})
    assert code == 0
    assert lp.actions() == [LAUNCH, COMPACT, LAUNCH, COMPACT]
    assert len(lp.events("compact_failed")) == 2


def test_attack_r8c_3_the_failure_line_carries_a_tail_not_all_of_stderr(loop, capsys):
    """The failure line is "the tail of stderr": the last three lines, joined.
    One 5 MB stderr line (a CLI dumping a response body) is printed whole to
    the run's terminal/log. The event is capped at 500 characters; the printed
    line is not."""
    lp = loop()
    lp.reading(OVER)
    _survives(lp, turns=[productive()], max_turns=1,
              compact={"exit": 1, "stderr_bytes": 5_000_000})
    failed = [line for line in capsys.readouterr().out.splitlines()
              if line.startswith("compaction failed")]
    assert len(failed) == 1
    assert len(failed[0]) <= 4_000, f"the failure line is {len(failed[0]):,} chars"


# ---------------------------------------------------------- R8c.3, timeout --

def test_attack_r8c_3_a_timed_out_compaction_does_not_run_on_into_the_next_turn(
        loop, tmp_path):
    """On timeout `subprocess.run` kills the script — only the script. The
    shipped `claude.sh compact` runs the CLI as its child (`out=$("$BIN" -p
    /compact --resume "$sid" ...)`), so the CLI keeps compacting the session
    after the driver has reported `compact_failed` and launched the next turn
    on the SAME session: two writers on one transcript, the thing R8c exists
    to avoid. A timeout must end the attempt, not abandon it."""
    marker = tmp_path / "orphan-finished"
    lp = loop(timeout=1)
    lp.reading(OVER)
    _survives(lp, turns=[productive()], max_turns=1,
              compact={"exit": 0, "sleep": 30, "orphan": str(marker),
                       "orphan_after": 3})
    assert len(lp.events("compact_failed")) == 1
    time.sleep(4)
    assert not marker.exists(), (
        "the compaction's child process outlived the timeout and kept working "
        "on the session")


# ------------------------------------------------------------ config shape --

@pytest.mark.parametrize("key,value", [("compact_at_tokens", "120k"),
                                       ("compact_timeout_seconds", "3m")])
def test_attack_r8c_1_a_malformed_limit_does_not_crash_the_run(loop, key, value):
    """`int(config.limits.get(...))` with no guard: a YAML value like `120k`
    raises ValueError out of `_supervise` after the first successful turn.
    The server reads the same keys through a guarded `_limit` (bad value ->
    0, disabled) — the driver should fail the same way, not end the run."""
    lp = loop()
    lp.config.project["limits"][key] = value
    lp.reading(OVER)
    code = _survives(lp, turns=[productive()], max_turns=2)
    assert code == 0
    assert lp.actions().count(LAUNCH) == 2
