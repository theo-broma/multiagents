"""C3 — the generic transport machinery, driven by a user-style custom provider.

Contract: `context/specs/c3-prompt-file-transport.md`, ids PF-R1, PF-R3, PF-R3a,
PF-R4, PF-R5, PF-R6, PF-R7 (the "Revision after the advisor's check" wins).

A custom provider (`cust`) declares each transport in turn and its native is the
fake of `tests/support/pf_harness.py`: it logs argv, all of stdin as bytes, and
the bytes of any file named in argv. The names of the config option, its
values and the file placeholder are the implementer's choice and live in the
marked constant block at the top of `pf_harness.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import pf_harness as pf  # noqa: E402

EXECUTORS = ["local", "docker"]
KIB = 1024

STDIN_ARGS = ["--flag"]
FILE_ARGS = ["--in", pf.FILE_PLACEHOLDER]
ARGV_ARGS = ["-p", "{prompt}"]
TRANSPORTS = {
    "stdin": (pf.TRANSPORT_STDIN, STDIN_ARGS),
    "file": (pf.TRANSPORT_FILE, FILE_ARGS),
    "argv": (pf.TRANSPORT_ARGV, ARGV_ARGS),
}


def _marker(tag: str) -> str:
    return f"PFMARK-{tag}-91be04d7"


def _rig(tmp_path, monkeypatch, kind: str, **kw) -> "pf.Rig":
    transport, args = TRANSPORTS[kind]
    return pf.custom_rig(tmp_path, monkeypatch, transport, args, **kw)


def _started(rig: "pf.Rig", task: str) -> str:
    result = rig.start(rig.name, task)
    assert isinstance(result, dict) and result.get("agent_id"), result
    node = rig.node(result["agent_id"])
    assert node.status == "done", f"{node.status}: {node.reason!r} / {result.get('error')!r}"
    return result["agent_id"]


def _payload(call: dict) -> bytes:
    """What the native received as the prompt, by the transport it declared."""
    if call["files"]:
        (data,) = call["files"].values()
        return data
    return call["stdin"]


def _diagnostics(rig: "pf.Rig", agent_id: str, result: dict) -> str:
    parts = [str(result.get("error", "")), str(result.get("reason", ""))]
    node = rig.node(agent_id)
    if node is not None:
        parts.append(node.reason or "")
    err = rig.run_dir(agent_id) / "stderr.log"
    if err.is_file():
        parts.append(err.read_text(errors="replace"))
    return "\n".join(parts)


def _steered(result: dict) -> bool:
    return not result.get("error") or (
        result.get("status") == "done"
        and str(result["error"]).startswith("the respawned run ended immediately"))


# ---------------------------------------------------------------------------
# PF-R1: a declared prompt transport
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
def test_pf_r1_stdin_transport_delivers_the_exact_bytes_and_keeps_them_out_of_argv(
        tmp_path, monkeypatch, executor):
    rig = _rig(tmp_path, monkeypatch, "stdin", executor=executor)
    marker = _marker("stdin")
    agent_id = _started(rig, marker + "\n" + pf.big_text())
    (call,) = rig.natives[rig.name].calls()
    expected = rig.prompt_md(agent_id)
    assert len(expected) > 128 * KIB
    assert call["stdin"] == expected, f"stdin: {len(call['stdin'])} bytes, want {len(expected)}"
    assert all(marker not in a for a in call["argv"])
    assert pf.argv_bytes(call) < 64 * KIB


@pytest.mark.parametrize("executor", EXECUTORS)
def test_pf_r1_file_transport_hands_a_path_in_the_run_dir_holding_the_exact_bytes(
        tmp_path, monkeypatch, executor):
    rig = _rig(tmp_path, monkeypatch, "file", executor=executor)
    marker = _marker("file")
    agent_id = _started(rig, marker + "\n" + pf.big_text())
    (call,) = rig.natives[rig.name].calls()
    expected = rig.prompt_md(agent_id)
    assert len(call["files"]) == 1, f"argv names {len(call['files'])} readable files: {call['argv']}"
    (path,) = call["paths"]
    (data,) = call["files"].values()
    assert data == expected, f"file holds {len(data)} bytes, want {len(expected)}"
    run_dir = rig.run_dir(agent_id).resolve()
    assert run_dir in Path(os.path.realpath(path)).parents, (
        f"the prompt file {path} is not in the run directory {run_dir}")
    assert all(marker not in a for a in call["argv"])
    assert pf.argv_bytes(call) < 64 * KIB
    assert pf.FILE_PLACEHOLDER not in " ".join(call["argv"])


@pytest.mark.parametrize("kind", ["stdin", "file"])
def test_pf_r1_text_that_looks_like_a_placeholder_is_delivered_literally(
        tmp_path, monkeypatch, kind):
    rig = _rig(tmp_path, monkeypatch, kind)
    task = "{prompt} {prompt_file} {model} {workdir} {session_id} %s %(x)s ${HOME}"
    agent_id = _started(rig, task)
    (call,) = rig.natives[rig.name].calls()
    assert _payload(call) == rig.prompt_md(agent_id)
    assert task.encode() in _payload(call)


@pytest.mark.parametrize("kind", ["stdin", "file"])
def test_pf_r1_trailing_newlines_and_metacharacters_survive(tmp_path, monkeypatch, kind):
    """A steer message is delivered verbatim: leading whitespace, trailing
    newlines (a shell `$(cat file)` would lose them) and shell metacharacters."""
    rig = _rig(tmp_path, monkeypatch, kind)
    agent_id = _started(rig, "first")
    message = "  \n\t lead $(touch /tmp/pf-pwn2) `id` ; | & > < ' \" \\ é 😀 \r\n\n\n"
    result = rig.steer(agent_id, message)
    assert _steered(result), result
    calls = rig.natives[rig.name].calls()
    assert len(calls) == 2
    assert _payload(calls[1]) == message.encode()
    assert not Path("/tmp/pf-pwn2").exists()


def test_pf_r1_empty_and_whitespace_only_messages_are_delivered_not_dropped(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin")
    agent_id = _started(rig, "first")
    result = rig.steer(agent_id, " \n ")
    calls = rig.natives[rig.name].calls()
    if len(calls) == 2:                      # steered at all: then verbatim
        assert calls[1]["stdin"] == b" \n "
    else:                                    # refused up front: with a reason
        assert result.get("error"), result


# ---------------------------------------------------------------------------
# PF-R6: the argv transport is today's behaviour for short prompts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["argv", "undeclared"])
def test_pf_r6_argv_transport_short_prompt_is_one_argv_element_as_before(
        tmp_path, monkeypatch, kind):
    if kind == "undeclared":
        rig = pf.custom_rig(tmp_path, monkeypatch, None, ARGV_ARGS)
    else:
        rig = _rig(tmp_path, monkeypatch, "argv")
    agent_id = _started(rig, "short task with {model} and $(x)")
    (call,) = rig.natives[rig.name].calls()
    prompt = rig.prompt_md(agent_id).decode()
    assert call["argv"][call["argv"].index("-p") + 1] == prompt
    assert call["stdin"] == b""
    assert rig.node(agent_id).session_id == "S-custom"


# ---------------------------------------------------------------------------
# PF-R4: the argv transport stays bounded, with a clear error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["argv", "undeclared"])
def test_pf_r4_argv_transport_200kib_is_refused_before_spawn_naming_the_option(
        tmp_path, monkeypatch, kind):
    if kind == "undeclared":
        rig = pf.custom_rig(tmp_path, monkeypatch, None, ARGV_ARGS)
    else:
        rig = _rig(tmp_path, monkeypatch, "argv")
    result = rig.start(rig.name, pf.big_text())
    message = str(result.get("error", ""))
    assert message, f"a 200 KiB argv prompt was not refused: {result}"
    assert pf.TRANSPORT_OPTION in message, (
        f"the error does not name the transport option {pf.TRANSPORT_OPTION!r}: {message}")
    assert pf.TRANSPORT_STDIN in message or pf.TRANSPORT_FILE in message, message
    assert rig.natives[rig.name].calls() == [], "a process was spawned"
    agent_id = result.get("agent_id")
    if agent_id:
        assert not (rig.run_dir(agent_id) / "wrapper.pid").exists(), "the wrapper started"
        assert not (rig.run_dir(agent_id) / "output.ndjson").exists()


def test_pf_r4_steer_over_the_argv_limit_is_refused_and_the_old_turn_input_untouched(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "argv")
    agent_id = _started(rig, "first")
    result = rig.steer(agent_id, pf.big_text())
    assert pf.TRANSPORT_OPTION in str(result.get("error", "")), result
    assert len(rig.natives[rig.name].calls()) == 1


# ---------------------------------------------------------------------------
# PF-R3 / PF-R3a: turn-specific, safe, bounded
# ---------------------------------------------------------------------------

def test_pf_r3_each_turn_has_its_own_owner_only_file_kept_for_the_runs_life(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "file")
    agent_id = _started(rig, "the first task")
    message = "the second turn, different text " + _marker("t2")
    result = rig.steer(agent_id, message)
    assert _steered(result), result
    first, second = rig.natives[rig.name].calls()
    assert first["paths"] and second["paths"], "the native was not handed a prompt file path"
    (p0,), (p1,) = first["paths"], second["paths"]
    assert p0 != p1, "two turns share one prompt file"
    run_dir = rig.run_dir(agent_id).resolve()
    for p in (p0, p1):
        real = Path(os.path.realpath(p))
        assert run_dir in real.parents, f"{p} is outside the run dir"
        st = os.lstat(p)
        assert stat.S_ISREG(st.st_mode), f"{p} is not a regular file"
        assert stat.S_IMODE(st.st_mode) == 0o600, oct(stat.S_IMODE(st.st_mode))
    # the earlier turn's file survives the later turn, unchanged
    assert Path(p0).read_bytes() == _payload(first)
    assert _payload(second) == message.encode()
    assert Path(p1).read_bytes() == message.encode()


def test_pf_r3_stdin_transport_also_keeps_a_per_turn_file_in_the_run_dir(
        tmp_path, monkeypatch):
    """The core writes the prompt to a per-turn file BEFORE handing it over, by
    any non-argv transport: after two turns the run dir holds two distinct
    owner-only regular files whose bytes are the two prompts."""
    rig = _rig(tmp_path, monkeypatch, "stdin")
    agent_id = _started(rig, "the first task")
    message = "second turn text " + _marker("stdin-t2")
    result = rig.steer(agent_id, message)
    assert _steered(result), result
    holders = {}
    for p in rig.run_files(agent_id):
        if p.is_symlink() or not p.is_file():
            continue
        if stat.S_IMODE(p.stat().st_mode) != 0o600:
            continue
        try:
            holders[p] = p.read_bytes()
        except OSError:
            continue
    second = [p for p, b in holders.items() if b == message.encode()]
    assert len(second) == 1, f"no owner-only 0600 file holds exactly the turn's prompt: {sorted(holders)}"
    first_prompt = rig.prompt_md(agent_id)
    firsts = [p for p, b in holders.items() if b == first_prompt]
    assert firsts and firsts[0] != second[0], "the first turn's file is gone or shared"


def test_pf_r3_a_symlink_planted_at_the_next_prompt_path_is_not_followed(
        tmp_path, monkeypatch):
    """Symlinks to a victim file are planted at every plausible name of the next
    turn's prompt file (the first turn's name with each number bumped, and the
    `prompt.N.md` family). A run-file write that followed one would overwrite
    the victim, or hand the native the victim's bytes. NOTE: the naming scheme
    is the implementer's; a name this test does not guess is not exercised."""
    import re
    rig = _rig(tmp_path, monkeypatch, "file")
    agent_id = _started(rig, "the first task")
    assert rig.natives[rig.name].calls()[0]["paths"], "the native was not handed a prompt file path"
    (p0,) = rig.natives[rig.name].calls()[0]["paths"]
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"VICTIM-ORIGINAL")
    run_dir = rig.run_dir(agent_id)
    base = os.path.basename(p0)
    names = {re.sub(r"\d+", lambda m: str(int(m.group()) + k), base)
             for k in (1, 2) if re.search(r"\d+", base)}
    names |= {f"prompt.{i}.md" for i in range(0, 5)} | {f"prompt-{i}.md" for i in range(0, 5)}
    names.discard(base)
    names.discard("prompt.md")
    for name in names:
        link = run_dir / name
        if not (link.exists() or link.is_symlink()):
            os.symlink(victim, link)
    message = "after the plant " + _marker("symlink")
    result = rig.steer(agent_id, message)
    assert victim.read_bytes() == b"VICTIM-ORIGINAL", "a planted symlink was followed on write"
    calls = rig.natives[rig.name].calls()
    if len(calls) == 2:
        assert _payload(calls[1]) == message.encode(), "the native read through a planted symlink"
    else:
        assert result.get("error"), result       # refused cleanly, with a reason


BOUND = 4096


def test_pf_r3_the_bound_is_measured_in_utf8_bytes_and_limit_plus_one_is_refused(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin", bound=BOUND)
    agent_id = _started(rig, "first")
    native = rig.natives[rig.name]
    # exactly the bound: accepted, whole
    at_bound = "a" * BOUND
    assert _steered(rig.steer(agent_id, at_bound))
    calls = native.calls()
    assert len(calls) == 2, "a prompt of exactly the bound was refused"
    assert calls[1]["stdin"] == at_bound.encode()
    # one byte over: a clear error, and nothing truncated reaches the native
    over = "b" * (BOUND + 1)
    result = rig.steer(agent_id, over)
    text = _diagnostics(rig, agent_id, result).lower()
    assert any(w in text for w in ("limit", "bound", "too large", "too big", "exceed", "bytes")), text
    for call in native.calls()[2:]:
        assert call["stdin"] == over.encode(), (
            f"a truncated prompt ({len(call['stdin'])} of {len(over)} bytes) reached the native")
    assert result.get("error") or rig.node(agent_id).status == "failed", result


def test_pf_r3_multibyte_text_under_the_bound_in_characters_is_over_it_in_bytes(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin", bound=BOUND)
    agent_id = _started(rig, "first")
    native = rig.natives[rig.name]
    fits = "é" * (BOUND // 2)                      # exactly BOUND bytes, BOUND/2 chars
    assert _steered(rig.steer(agent_id, fits))
    assert native.calls()[-1]["stdin"] == fits.encode()
    n_before = len(native.calls())
    over = "é" * (BOUND // 2 + 1)                  # BOUND + 2 bytes, well under BOUND chars
    assert len(over) < BOUND < len(over.encode())
    result = rig.steer(agent_id, over)
    for call in native.calls()[n_before:]:
        assert call["stdin"] == over.encode(), "a truncated multibyte prompt reached the native"
    assert result.get("error") or rig.node(agent_id).status == "failed", (
        "a prompt over the byte bound was neither refused nor failed")


def test_pf_r3_the_default_bound_is_comfortably_above_a_realistic_prompt(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin")
    big = ("x" * 1023 + "\n") * 1024 * 4            # 4 MiB
    agent_id = _started(rig, big)
    (call,) = rig.natives[rig.name].calls()
    assert call["stdin"] == rig.prompt_md(agent_id)
    assert len(call["stdin"]) > 4 * 1024 * 1024


def test_pf_r3_past_the_default_bound_is_an_error_never_a_silent_truncation(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin")
    agent_id = _started(rig, "first")
    native = rig.natives[rig.name]
    huge = ("y" * 1023 + "\n") * 1024 * 17          # 17 MiB, past a 16 MiB default
    result = rig.steer(agent_id, huge)
    text = _diagnostics(rig, agent_id, result).lower()
    assert any(w in text for w in ("limit", "bound", "too large", "too big", "exceed", "bytes")), text
    for call in native.calls()[1:]:
        assert call["stdin"] == huge.encode(), (
            f"a truncated prompt ({len(call['stdin'])} bytes) reached the native")
    assert result.get("error") or rig.node(agent_id).status == "failed"


# ---------------------------------------------------------------------------
# PF-R5: ordering and a clean failure
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("kind", ["stdin", "file"])
def test_pf_r5_a_prompt_file_that_cannot_be_written_fails_the_launch_before_spawn(
        tmp_path, monkeypatch, kind):
    rig = _rig(tmp_path, monkeypatch, kind)
    agent_id = _started(rig, "first")
    native = rig.natives[rig.name]
    run_dir = rig.run_dir(agent_id)
    run_dir.chmod(0o500)
    try:
        result = rig.steer(agent_id, "cannot be written " + _marker("ro"))
    finally:
        run_dir.chmod(0o700)
    assert len(native.calls()) == 1, "a process was spawned although the prompt file could not be written"
    error = str(result.get("error", ""))
    assert result.get("steered") is False and error, result
    assert any(w in error.lower() for w in ("permission", "read-only", "denied", "prompt", "write")), (
        f"the error does not say why: {error!r}")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_pf_r7_a_failed_launch_followed_by_another_launch_uses_the_right_input(
        tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, "stdin")
    agent_id = _started(rig, _marker("ORIGINAL"))
    native = rig.natives[rig.name]
    run_dir = rig.run_dir(agent_id)
    lost = "this launch fails " + _marker("LOST")
    run_dir.chmod(0o500)
    try:
        first = rig.steer(agent_id, lost)
    finally:
        run_dir.chmod(0o700)
    assert first.get("error"), first
    wanted = "this one must be the input " + _marker("WANTED")
    second = rig.steer(agent_id, wanted)
    assert _steered(second), second
    calls = native.calls()
    assert len(calls) == 2, f"{len(calls)} native invocations"
    assert calls[1]["stdin"] == wanted.encode()
    assert _marker("LOST").encode() not in calls[1]["stdin"]
    assert _marker("ORIGINAL").encode() not in calls[1]["stdin"]


# ---------------------------------------------------------------------------
# PF-R7: each launch attempt has its own durable input; retries replay it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["stdin", "file", "argv"])
def test_pf_r7_a_steer_whose_turn_dies_silently_is_retried_with_the_steers_text(
        tmp_path, monkeypatch, kind):
    rig = _rig(tmp_path, monkeypatch, kind)
    original = _marker("ORIGINAL-TASK")
    agent_id = _started(rig, original)
    native = rig.natives[rig.name]
    native.behave(exit_by_call={"1": 1})              # the steer's turn dies, silent
    steer_mark = _marker("STEER-TEXT")
    rig.steer(agent_id, steer_mark + " do the other thing")
    calls = native.calls()
    assert len(calls) == 3, f"{len(calls)} native invocations (start, steer, retry)"
    retry = pf.delivered_anywhere(calls[2])
    assert steer_mark.encode() in retry, "the retry did not replay the steer's text"
    assert original.encode() not in retry, "the retry replayed the original task"
    assert rig.node(agent_id).status == "done"


def test_pf_r7_the_retry_replays_through_a_fresh_immutable_file(tmp_path, monkeypatch):
    """A retry gets its own prompt file: neither the steer's nor the start's
    file is reused or rewritten."""
    rig = _rig(tmp_path, monkeypatch, "file")
    agent_id = _started(rig, _marker("ORIGINAL-TASK"))
    native = rig.natives[rig.name]
    native.behave(exit_by_call={"1": 1})
    steer_text = "steer " + _marker("STEER-TEXT")
    rig.steer(agent_id, steer_text)
    calls = native.calls()
    assert len(calls) == 3
    assert all(c["paths"] for c in calls), "the native was not handed a prompt file path"
    paths = [c["paths"][0] for c in calls]
    assert len(set(paths)) == 3, f"prompt files are reused across attempts: {paths}"
    assert Path(paths[0]).read_bytes() == calls[0]["files"][paths[0]], "the first file was rewritten"
    assert Path(paths[1]).read_bytes() == calls[1]["files"][paths[1]], "the steer's file was rewritten"
    assert calls[2]["files"][paths[2]] == steer_text.encode()


def test_pf_r7_a_draining_predecessors_prompt_file_is_not_overwritten(
        tmp_path, monkeypatch):
    """The predecessor is told to stop but is still reading its own prompt file
    while it drains (it re-reads it after SIGTERM). A steer's launch must not
    have rewritten that file."""
    rig = _rig(tmp_path, monkeypatch, "file")
    native = rig.natives[rig.name]
    native.behave(hold_first=True, drain_seconds=1.0)
    first_text = _marker("FIRST-TASK")

    async def scenario():
        started = await rig.runner.start(rig.name, first_text)
        agent_id = started["agent_id"]
        deadline = time.monotonic() + 20
        while not native.calls() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert native.calls(), "the predecessor never started"
        while not rig.node(agent_id).session_id and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        rig.ensure_transcripts()
        steered = await rig.runner.steer(agent_id, "steer text " + _marker("STEER"))
        await rig._settle(agent_id)
        return agent_id, steered

    agent_id, steered = rig.run_async(scenario())
    calls = native.calls()
    assert len(calls) == 2, (len(calls), steered)
    assert calls[0]["paths"] and calls[1]["paths"], "the native was not handed a prompt file path"
    p0, p1 = calls[0]["paths"][0], calls[1]["paths"][0]
    assert p0 != p1, "the steer reused the draining predecessor's prompt file"
    reread = pf.rereads(native)
    assert reread, "the predecessor never got to re-read its file"
    assert reread[0]["bytes"] == calls[0]["files"][p0], (
        "the predecessor's prompt file changed while it was draining")
    assert first_text.encode() in reread[0]["bytes"]
