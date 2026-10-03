"""C3 — the shipped providers receive the prompt through the run-file transport.

Contract: `context/specs/c3-prompt-file-transport.md`, ids PF-R1a, PF-R2,
PF-R2a, PF-R6, PF-R7 (the "Revision after the advisor's check" wins).

The whole real path runs: `Runner.start / steer / consult` -> `_launch` -> the
SHIPPED provider block (only its `bin` is pointed at a fake native) -> the
adapter (codex) -> `agentwrap` -> the executor (local, or docker via the
executing fake `docker` of `sp_harness`) -> a fake native binary that reads
`sys.stdin.buffer.read()` and logs argv + stdin bytes. See
`tests/support/pf_harness.py`.

Required, per PF-R1a: claude, codex, agy and opencode receive the prompt on
STDIN (agy as `--input-format stream-json`, whose decoded message must equal
the prompt byte for byte). A provider whose installed CLI cannot do that is a
documented PF-R2 exception; this suite has no way to know of one and holds all
four to stdin.

Red today: every shipped provider but codex puts the prompt in argv and gives
the child /dev/null as stdin; codex's adapter reads it from ITS argv, and a
prompt over 128 KiB is refused outright by H8.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import pf_harness as pf  # noqa: E402

EXECUTORS = ["local", "docker"]
CORE = ["claude", "codex", "agy", "opencode"]
VARIANTS = ["opencode-zai", "opencode-deepinfra", "agy-partner"]
KIB = 1024


def _marker(tag: str) -> str:
    return f"PFMARK-{tag}-7f3a91c2"


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------

def _delivered_on_stdin(call: dict, provider: str, expected: bytes) -> bool:
    return expected in pf.delivered_by_stdin(call, provider) if provider.startswith("agy") \
        else call["stdin"] == expected


def assert_on_stdin(call: dict, provider: str, expected: bytes) -> None:
    assert _delivered_on_stdin(call, provider, expected), (
        f"{provider}: the native's stdin ({len(call['stdin'])} bytes) is not the prompt "
        f"({len(expected)} bytes) byte for byte; first 80 bytes of stdin: "
        f"{call['stdin'][:80]!r}")


def assert_not_in_argv(rig: "pf.Rig", agent_id: str, marker: str) -> None:
    """The prompt text is in no argv: not the native's, not the adapter's or
    wrapper's (command.json), not the `docker exec` line."""
    needle = marker.encode()
    for name, native in rig.natives.items():
        for call in native.calls():
            for element in call["argv"]:
                assert needle not in element.encode("utf-8", "surrogateescape"), (
                    f"{name}: the prompt is in the native's argv ({len(element)} chars)")
            assert pf.argv_bytes(call) < 64 * KIB, "argv is huge: the prompt is in it"
    command = rig.run_dir(agent_id) / "command.json"
    if command.is_file():
        for element in json.loads(command.read_text())["argv"]:
            assert marker not in element, (
                "command.json: the prompt is in the launch argv "
                f"({len(element)} chars)")
    if rig.executor == "docker":
        for call in pf.sp.docker_calls(rig.docker_log):
            assert not any(marker in part for part in call), (
                "the prompt is in a `docker` command line")


def _ok(rig: "pf.Rig", result: dict) -> str:
    assert isinstance(result, dict) and result.get("agent_id"), result
    agent_id = result["agent_id"]
    node = rig.node(agent_id)
    assert node.status == "done", f"{node.status}: {node.reason!r} / {result.get('error')!r}"
    return agent_id


def assert_steered(result: dict) -> None:
    """A steer that was launched. The core's own confirm window answers "the
    respawned run ended immediately" for a turn that finished before its first
    event was read, even though the node is `done` (a race this contract does
    not own), so that one answer is accepted."""
    if result.get("error") and not (
            result.get("status") == "done"
            and str(result["error"]).startswith("the respawned run ended immediately")):
        raise AssertionError(result)


STEER_TEXT = ("  \t\n   leading whitespace is part of the message\n"
              + pf.big_text() + "\n\n\n")


# ---------------------------------------------------------------------------
# PF-R2 / PF-R1a / PF-R2a: a fresh start, over 128 KiB, both executors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", CORE)
def test_pf_r2_fresh_start_over_128kib_reaches_the_native_byte_for_byte(
        tmp_path, monkeypatch, provider, executor):
    rig = pf.Rig(tmp_path, monkeypatch, executor=executor, names=(provider,))
    marker = _marker("fresh")
    task = marker + "\n" + pf.big_text()
    result = rig.start(provider, task)
    agent_id = _ok(rig, result)       # fails with the launch error if refused
    expected = rig.prompt_md(agent_id)                    # the composed prompt
    assert len(expected) > 128 * KIB
    assert expected.endswith(task.strip().encode() + b"\n")
    calls = rig.natives[provider].calls()
    assert len(calls) == 1, f"{len(calls)} native invocations"
    assert_on_stdin(calls[0], provider, expected)
    assert_not_in_argv(rig, agent_id, marker)


@pytest.mark.parametrize("provider", VARIANTS)
def test_pf_r2_extends_variants_are_converted_too(tmp_path, monkeypatch, provider):
    rig = pf.Rig(tmp_path, monkeypatch, names=(provider,))
    marker = _marker("variant")
    result = rig.start(provider, marker + "\n" + pf.big_text())
    agent_id = _ok(rig, result)
    expected = rig.prompt_md(agent_id)
    (call,) = rig.natives[provider].calls()
    assert_on_stdin(call, provider, expected)
    assert_not_in_argv(rig, agent_id, marker)


@pytest.mark.parametrize("provider", CORE)
def test_pf_r2a_a_short_prompt_travels_the_same_way(tmp_path, monkeypatch, provider):
    """PF-R2: "no longer carry the prompt in argv on any launch path" is not a
    size rule. A short prompt with leading text, metacharacters and the
    composed prompt's trailing newline arrives on stdin too."""
    rig = pf.Rig(tmp_path, monkeypatch, names=(provider,))
    marker = _marker("short")
    task = marker + " $(touch /tmp/pf-pwn) `id` 'q' \"dq\" ; | & > < \\ é 😀"
    result = rig.start(provider, task)
    agent_id = _ok(rig, result)
    (call,) = rig.natives[provider].calls()
    expected = rig.prompt_md(agent_id)
    assert expected.endswith(b"\n")
    assert_on_stdin(call, provider, expected)
    assert_not_in_argv(rig, agent_id, marker)
    assert not Path("/tmp/pf-pwn").exists(), "a shell expanded the prompt"


# ---------------------------------------------------------------------------
# PF-R2: resume (steer), both executors; leading whitespace + trailing newlines
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", CORE)
def test_pf_r2_resume_over_128kib_reaches_the_native_byte_for_byte(
        tmp_path, monkeypatch, provider, executor):
    rig = pf.Rig(tmp_path, monkeypatch, executor=executor, names=(provider,))
    agent_id = _ok(rig, rig.start(provider, "the first, short task"))
    marker = _marker("steer")
    message = marker + STEER_TEXT
    steered = rig.steer(agent_id, message)
    assert_steered(steered)
    calls = rig.natives[provider].calls()
    assert len(calls) == 2, f"{len(calls)} native invocations; steer result {steered}"
    # it is a RESUME of the first turn's session, not a fresh start
    assert any("S-" + provider in a for a in calls[1]["argv"]), calls[1]["argv"][:12]
    assert len(message.encode()) > 128 * KIB
    # verbatim: the message's own leading whitespace and trailing newlines
    assert_on_stdin(calls[1], provider, message.encode())
    assert_not_in_argv(rig, agent_id, marker)


@pytest.mark.parametrize("provider", CORE)
def test_pf_r2_consult_resume_reaches_the_native_whole(tmp_path, monkeypatch, provider):
    rig = pf.Rig(tmp_path, monkeypatch, names=(provider,), conversational=(provider,))
    first = rig.consult(provider, "first question")
    assert not first.get("error"), first
    marker = _marker("consult")
    message = marker + "\n" + pf.big_text()
    second = rig.consult(provider, message)
    assert not second.get("error"), second
    calls = rig.natives[provider].calls()
    assert len(calls) == 2, len(calls)
    assert any("S-" + provider in a for a in calls[1]["argv"]), "not a resume"
    cands = pf.delivered_by_stdin(calls[1], provider)
    assert any(message.encode() in c for c in cands), (
        "the consult text did not arrive whole on stdin "
        f"(stdin {len(calls[1]['stdin'])} bytes)")
    for call in calls:
        assert all(marker not in a for a in call["argv"])


@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", ["claude", "agy", "opencode"])   # codex's adapter
# always reports a failed turn in-band, so its death is never "silent"
def test_pf_r2_free_retry_reaches_the_native_by_stdin_too(
        tmp_path, monkeypatch, provider, executor):
    """The free retry (a silent early death, relaunched once) is one of the
    launch paths PF-R2 lists. The first attempt dies without a word; the retry
    delivers the whole >128 KiB prompt on stdin."""
    rig = pf.Rig(tmp_path, monkeypatch, executor=executor, names=(provider,))
    rig.natives[provider].behave(exit_by_call={"0": 1})
    marker = _marker("retry")
    result = rig.start(provider, marker + "\n" + pf.big_text())
    agent_id = _ok(rig, result)
    expected = rig.prompt_md(agent_id)
    calls = rig.natives[provider].calls()
    assert len(calls) == 2, f"{len(calls)} native invocations (want first + one retry)"
    assert_on_stdin(calls[1], provider, expected)
    assert_not_in_argv(rig, agent_id, marker)
    assert rig.node(agent_id).status == "done"


@pytest.mark.parametrize("executor", EXECUTORS)
def test_pf_r2_commit_fix_resume_reaches_the_native_by_stdin(
        tmp_path, monkeypatch, executor):
    """The commit-fix turn (a hook refused the agent's commit) resumes the
    session with a prompt that embeds the hook's output; that prompt is not in
    argv either."""
    provider = "claude"
    rig = pf.Rig(tmp_path, monkeypatch, executor=executor, names=(provider,))
    hook_mark = _marker("hook")
    hook = Path(rig.runner.paths.root) / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho {hook_mark} >&2\nexit 1\n")
    hook.chmod(0o755)
    rig.natives[provider].behave(touch="agent-work.txt")
    result = rig.start(provider, "do some work and leave it uncommitted")
    assert result.get("agent_id"), result
    calls = rig.natives[provider].calls()
    assert len(calls) >= 2, (
        f"no commit-fix turn ran ({len(calls)} native invocation); "
        f"node: {rig.node(result['agent_id']).status} {rig.node(result['agent_id']).reason!r}")
    fix = calls[1]
    assert any("S-claude" in a for a in fix["argv"]), "the fix turn is not a resume"
    assert hook_mark.encode() in fix["stdin"], (
        "the commit-fix prompt (which quotes the hook's output) is not on stdin")
    assert all(hook_mark not in a for a in fix["argv"])


# ---------------------------------------------------------------------------
# PF-R1a: per-provider specifics
# ---------------------------------------------------------------------------

def test_pf_r1a_agy_stdin_is_stream_json_whose_content_is_the_prompt(
        tmp_path, monkeypatch):
    rig = pf.Rig(tmp_path, monkeypatch, names=("agy",))
    marker = _marker("agyjson")
    task = marker + ' "quotes" \\ back\\slash   \n\ttabs 😀 é'
    agent_id = _ok(rig, rig.start("agy", task))
    (call,) = rig.natives["agy"].calls()
    argv = call["argv"]
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json", argv
    lines = [ln for ln in call["stdin"].decode("utf-8").split("\n") if ln.strip()]
    assert lines, "agy got nothing on stdin"
    docs = [json.loads(ln) for ln in lines]      # every line is a JSON document
    expected = rig.prompt_md(agent_id).decode()
    leaves: list[str] = []

    def walk(v):
        if isinstance(v, str):
            leaves.append(v)
        elif isinstance(v, dict):
            [walk(x) for x in v.values()]
        elif isinstance(v, list):
            [walk(x) for x in v]
    walk(docs)
    assert expected in leaves, "no message's decoded content equals the prompt"
    # never a system prompt or an attachment (PF-R1a)
    text = json.dumps(docs).lower()
    assert "system" not in {str(d.get("role", d.get("type", ""))).lower() for d in docs
                            if isinstance(d, dict)}, docs[0]
    assert "attachment" not in text and "file_path" not in text


def test_pf_r1a_opencode_prompt_is_the_message_text_never_a_file_attachment(
        tmp_path, monkeypatch):
    rig = pf.Rig(tmp_path, monkeypatch, names=("opencode",))
    marker = _marker("ocfile")
    agent_id = _ok(rig, rig.start("opencode", marker + "\n" + pf.big_text()))
    (call,) = rig.natives["opencode"].calls()
    assert "--file" not in call["argv"] and "-f" not in call["argv"], call["argv"]
    assert call["files"] == {}, "the native was handed a file path (an attachment)"
    assert call["stdin"] == rig.prompt_md(agent_id)


def test_pf_r1a_claude_runs_in_print_mode_with_the_prompt_on_stdin(
        tmp_path, monkeypatch):
    rig = pf.Rig(tmp_path, monkeypatch, names=("claude",))
    marker = _marker("clprint")
    agent_id = _ok(rig, rig.start("claude", marker))
    (call,) = rig.natives["claude"].calls()
    assert "-p" in call["argv"] or "--print" in call["argv"], call["argv"]
    assert call["stdin"] == rig.prompt_md(agent_id)
    # the prompt is not the value of -p either
    assert all(marker not in a for a in call["argv"])


def test_pf_r1a_codex_adapter_takes_the_prompt_from_the_run_file_not_its_argv(
        tmp_path, monkeypatch):
    rig = pf.Rig(tmp_path, monkeypatch, names=("codex",))
    marker = _marker("cxargv")
    agent_id = _ok(rig, rig.start("codex", marker + "\n" + pf.big_text()))
    (call,) = rig.natives["codex"].calls()
    assert call["stdin"] == rig.prompt_md(agent_id)
    assert_not_in_argv(rig, agent_id, marker)


# ---------------------------------------------------------------------------
# PF-R6: no regression, visible to the agent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", CORE)
def test_pf_r6_session_id_prompt_md_and_resume_still_work(tmp_path, monkeypatch, provider):
    rig = pf.Rig(tmp_path, monkeypatch, names=(provider,))
    agent_id = _ok(rig, rig.start(provider, "small task"))
    node = rig.node(agent_id)
    assert node.session_id == "S-" + provider              # stream parsing
    md = rig.prompt_md(agent_id).decode()
    assert md.endswith("## Task\n\nsmall task\n")           # diagnostics intact
    steered = rig.steer(agent_id, "next step")
    assert_steered(steered)
    assert rig.node(agent_id).status == "done"
    assert rig.node(agent_id).session_id == "S-" + provider
    assert len(rig.natives[provider].calls()) == 2


# ---------------------------------------------------------------------------
# PF-R7 on the shipped providers: a retry replays ITS attempt's input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor,provider", [
    ("local", "claude"), ("docker", "claude"), ("local", "agy"), ("local", "opencode")])
def test_pf_r7_a_failed_steer_turn_is_retried_with_the_steers_text(
        tmp_path, monkeypatch, provider, executor):
    rig = pf.Rig(tmp_path, monkeypatch, executor=executor, names=(provider,))
    original = _marker("ORIGINAL-TASK")
    agent_id = _ok(rig, rig.start(provider, original))
    steer_mark = _marker("STEER-TEXT")
    # call 0 was the start; the steer's turn (call 1) dies silently, so the
    # free retry (call 2) relaunches.
    rig.natives[provider].behave(exit_by_call={"1": 1})
    rig.steer(agent_id, steer_mark + " now do the other thing")
    calls = rig.natives[provider].calls()
    assert len(calls) == 3, f"{len(calls)} native invocations (start, steer, retry)"
    retry = pf.delivered_anywhere(calls[2], provider)
    assert steer_mark.encode() in retry, "the retry did not replay the steer's text"
    assert original.encode() not in retry, "the retry replayed the ORIGINAL task"
    assert rig.node(agent_id).status == "done"
