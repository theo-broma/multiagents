"""CX-C28 — a conversation is never resumed on a provider the roster no longer
allows (context/specs/codex-provider.md, "Advisor catch-up and final review").

Observed live at L7: after the roster moved `advisor` off one provider, the
first `consult("advisor")` resumed the old conversation on the old provider,
spent a turn there, and answered as the old model. The contract:

- a standing conversational node whose provider is neither the roster's
  current provider for that agent nor one of its `models:` fallbacks is not
  resumed;
- instead a new conversation starts on the current roster, an event
  `conversation_replaced` records the old node id, and the reply carries a
  one-line note that the previous context was not carried over;
- a fallback that resolves to an empty model is not a valid route, and is
  refused rather than run.

Everything is observed from outside: which fake CLI binary actually ran and
with what argv, what `consult()` returned, what the tree holds, and what the
event log says. Provider names are invented ("acme", "zeta") so nothing here
depends on a real CLI.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402

OLD_SESSION = "sess-acme-old"
OLD_NODE = "ag-0ld0ac"

# One fake CLI per provider, each its own binary, so "provider A's bin never
# ran" is a fact about the filesystem rather than about the runner's choices.
# Every invocation writes its argv to a numbered file under its own probe
# directory, then answers once, naming itself and a session id of its own.
_CLI = """#!/bin/sh
probe="@PROBE@"
n=$(ls "$probe" | wc -l)
printf '%s\\n' "$@" > "$probe/.part"
mv "$probe/.part" "$probe/call-$n.argv"
printf '%s\\n' '{"type":"text","text":"answered by @NAME@","session":"sess-@NAME@-new"}'
exit 0
"""


def _fake_cli(tmp_path: Path, name: str) -> tuple[dict, Path]:
    probe = tmp_path / f"probe-{name}"
    probe.mkdir()
    script = tmp_path / f"{name}-cli"
    script.write_text(_CLI.replace("@PROBE@", str(probe)).replace("@NAME@", name))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    provider = {
        "bin": str(script),
        "spawn": {"args": ["--provider", name, "--model", "{model}",
                           "--cwd", "{workdir}"],
                  "resume": ["--resume", "{session_id}"],
                  "optional": {"effort": ["--effort", "{effort}"]}},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }
    return provider, probe


def _calls(probe: Path) -> list[list[str]]:
    """Every argv this fake was run with, in order."""
    files = sorted(probe.glob("call-*.argv"),
                   key=lambda p: int(p.stem.split("-")[1]))
    # One line per argument, each newline-terminated: an empty argument (an
    # empty `--model`) is an empty line and must survive the split.
    return [f.read_text().split("\n")[:-1] for f in files]


def _flag(argv, flag):
    assert flag in argv, f"{flag} missing from {argv}"
    return argv[argv.index(flag) + 1]


def _events(runner, kind):
    path = runner.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") == kind:
            out.append(entry)
    return out


class Moved:
    """An `advisor` whose standing conversation was created on `acme`, and a
    roster that now names `zeta` — with whatever `models:` fallbacks the test
    gives it."""

    def __init__(self, tmp_path, monkeypatch, *, models=None,
                 worktree_in_domain=False):
        acme, self.acme_probe = _fake_cli(tmp_path, "acme")
        zeta, self.zeta_probe = _fake_cli(tmp_path, "zeta")
        self.acme_bin = acme["bin"]
        spec = AgentSpec("advisor", "zeta", "zeta-large", conversational=True,
                         models=models or {})
        self.root = tmp_path / "proj"
        self.runner = h.make_runner(self.root, monkeypatch,
                                    agents={"advisor": spec},
                                    providers={"acme": acme, "zeta": zeta})
        worktree = (self.runner.paths.worktree(OLD_NODE) if worktree_in_domain
                    else tmp_path / "old-worktree")
        worktree.mkdir(parents=True)
        # The conversation as the earlier roster left it: on acme, idle, one
        # turn in, with acme's session.
        self.runner.tree.add(Node(
            id=OLD_NODE, agent="advisor", provider="acme", model="acme-large",
            parent=None, depth=1, status="idle", session_id=OLD_SESSION,
            worktree=str(worktree), conversation=True, turns=1))

    def consult(self, message="next question"):
        return asyncio.run(self.runner.consult("advisor", message, timeout=60))

    def all_argv(self):
        return _calls(self.acme_probe) + _calls(self.zeta_probe)


# ---------------------------------------------------------------------------
# 1. The roster moved to zeta, and acme is not a fallback
# ---------------------------------------------------------------------------

def test_cx_c28_a_dropped_providers_session_is_never_resumed(tmp_path, monkeypatch):
    """The L7 defect itself: the old provider must not spend a turn, and its
    session must not travel to the new one either."""
    m = Moved(tmp_path, monkeypatch)

    result = m.consult()

    assert not _calls(m.acme_probe), (
        f"acme's binary ran although the roster no longer allows acme for "
        f"advisor: {_calls(m.acme_probe)}; result={result}")
    for argv in m.all_argv():
        assert OLD_SESSION not in argv, (
            f"acme's session id was handed to a CLI: {argv}")
        assert m.acme_bin not in argv, f"acme's binary appears in argv: {argv}"


def test_cx_c28_a_new_conversation_starts_on_the_current_provider(tmp_path, monkeypatch):
    m = Moved(tmp_path, monkeypatch)

    result = m.consult()

    zeta = _calls(m.zeta_probe)
    assert len(zeta) == 1, (
        f"expected exactly one fresh turn on zeta, got {zeta}; result={result}")
    argv = zeta[0]
    assert "--resume" not in argv, (
        f"a new conversation has nothing to resume: {argv}")
    assert _flag(argv, "--provider") == "zeta"
    assert _flag(argv, "--model") == "zeta-large"

    assert not result.get("error"), f"the consult failed: {result}"
    new_id = result.get("agent_id")
    assert new_id and new_id != OLD_NODE, (
        f"the reply must come from a new node, not {OLD_NODE}: {result}")
    node = m.runner.tree.get(new_id)
    assert node is not None, f"{new_id} is not in the tree"
    assert node.provider == "zeta"
    assert node.agent == "advisor"
    assert node.conversation


def test_cx_c28_the_replacement_is_recorded_with_the_old_node_id(tmp_path, monkeypatch):
    m = Moved(tmp_path, monkeypatch)

    result = m.consult()

    replaced = _events(m.runner, "conversation_replaced")
    assert len(replaced) == 1, (
        f"expected one conversation_replaced event, got {replaced}; "
        f"result={result}")
    assert OLD_NODE in json.dumps(replaced[0]), (
        f"the event must name the old node {OLD_NODE}: {replaced[0]}")


def test_cx_c28_the_reply_says_the_old_context_was_not_carried_over(tmp_path, monkeypatch):
    """The caller consulted what it believed was a standing conversation; it
    must learn from the reply itself that the memory it expected is gone."""
    m = Moved(tmp_path, monkeypatch)

    result = m.consult()

    reply = result.get("reply") or ""
    assert "answered by zeta" in reply, f"zeta's answer is missing: {result}"
    extra = [line for line in reply.splitlines()
             if line.strip() and "answered by zeta" not in line]
    assert len(extra) == 1, (
        f"expected a one-line note beside the answer, got {extra!r} in "
        f"{reply!r}")


def test_cx_c28_the_next_consult_continues_the_replacement(tmp_path, monkeypatch):
    """Replacing is a one-time event. The turn after it resumes the new
    conversation — not the old node, not a third node, and without
    announcing the replacement again."""
    m = Moved(tmp_path, monkeypatch)

    first = m.consult("first")
    second = m.consult("second")

    assert not _calls(m.acme_probe), f"acme ran: {_calls(m.acme_probe)}"
    zeta = _calls(m.zeta_probe)
    assert len(zeta) == 2, f"expected two zeta turns, got {zeta}"
    assert _flag(zeta[1], "--resume") == "sess-zeta-new", (
        f"the second turn must resume the replacement's session: {zeta[1]}")
    assert first.get("agent_id") and second.get("agent_id") == first.get("agent_id"), (
        f"the second consult went to a different node: {first} / {second}")
    assert len(_events(m.runner, "conversation_replaced")) == 1
    reply = second.get("reply") or ""
    assert [line for line in reply.splitlines() if line.strip()] == ["answered by zeta"], (
        f"the note belongs to the turn that replaced, not every turn: {reply!r}")


# ---------------------------------------------------------------------------
# 2. acme is still a fallback: resuming there is allowed
# ---------------------------------------------------------------------------

def test_cx_c28_a_conversation_on_a_listed_fallback_is_still_resumed(tmp_path, monkeypatch):
    """The other half, and the one an over-eager fix breaks: a node routed to
    a fallback the roster still lists keeps its conversation."""
    m = Moved(tmp_path, monkeypatch, models={"acme": "acme-large"},
              worktree_in_domain=True)

    result = m.consult()

    assert not _calls(m.zeta_probe), (
        f"a new zeta conversation was started although acme is an allowed "
        f"fallback: {_calls(m.zeta_probe)}")
    acme = _calls(m.acme_probe)
    assert len(acme) == 1, f"acme was not resumed: {acme}; result={result}"
    assert _flag(acme[0], "--resume") == OLD_SESSION
    assert _flag(acme[0], "--provider") == "acme"
    assert _flag(acme[0], "--model") == "acme-large"
    assert result.get("agent_id") == OLD_NODE
    assert not _events(m.runner, "conversation_replaced")


# ---------------------------------------------------------------------------
# 3. A fallback that resolves to an empty model is not a route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    pytest.param("", id="empty-string"),
    pytest.param({"effort": "low"}, id="options-without-a-model"),
    pytest.param({"model": ""}, id="explicit-empty-model"),
])
def test_cx_c28_an_empty_fallback_model_is_refused_not_run(tmp_path, monkeypatch, entry):
    """`models: {acme: ""}` names acme but gives it no model. Running the CLI
    with an empty `--model` is exactly the failure this rules out: whatever
    happens instead, nothing is run on acme, and no CLI is run with an empty
    model."""
    m = Moved(tmp_path, monkeypatch, models={"acme": entry})

    try:
        m.consult()
    except (ValueError, RuntimeError, FileNotFoundError, PermissionError):
        pass    # a refusal raised is a refusal; what matters is what ran

    assert not _calls(m.acme_probe), (
        f"acme ran with a fallback that has no model: {_calls(m.acme_probe)}")
    for argv in m.all_argv():
        assert _flag(argv, "--model") != "", f"a CLI ran with an empty model: {argv}"
        assert OLD_SESSION not in argv, f"acme's session travelled: {argv}"
