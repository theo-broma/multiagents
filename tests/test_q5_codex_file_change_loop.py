"""tests/test_q5_codex_file_change_loop.py — Q5, the doom-loop false
positive on codex `file_change` events.

Codex reports each file edit as a `file_change` item; the adapter's
Normalizer turns it into a tool event named `file_change` whose args carry
only the changed paths and kinds — never the content. A codex agent editing
the same file several times in a row therefore emits a stream of tool events
whose name-and-args signature is identical, and the doom-loop watchdog used
to trip on a working agent mid-edit.

The one field that would tell two edits apart — the item id, which the
Normalizer does track per turn (`seen_tools`) to avoid reporting one item
twice — is deliberately dropped from the event itself (CX-C26: only the
event shape is carried, never the raw object). With no argument that
distinguishes a legitimate edit from a repeated signature, the tool is
declared opaque whole in the codex block of `providers.yaml` — the same
reasoning as agy's `view_file` (SL-R6), not the arg-scoped `manage_task`
rule (bug-8615db, tests/test_doom_loop_polling.py), and the Supervisor is
unchanged.

These tests read the shipped `providers.yaml` codex block and wire the
Supervisor from it the way the runner does (`opaque_tools` and
`opaque_tool_args` handed to `Supervisor`), so the declaration itself — not
a copy of it — is what is under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.providers import Event                            # noqa: E402
from multiagents.supervisor import Supervisor                      # noqa: E402

BIG = 10 ** 9

SHIPPED_YAML = (Path(__file__).resolve().parents[1]
                / "src/multiagents/defaults/providers.yaml")


def codex_block() -> dict:
    return yaml.safe_load(SHIPPED_YAML.read_text())["providers"]["codex"]


def _sup(**kw) -> Supervisor:
    """A supervisor with every condition but doom_loop disabled, threshold 3."""
    kw.setdefault("silence_timeout", BIG)
    kw.setdefault("wall_timeout", BIG)
    kw.setdefault("max_steps", BIG)
    kw.setdefault("loop_repeats", 3)
    return Supervisor(**kw)


def _codex_sup(**kw) -> Supervisor:
    """A supervisor wired from the shipped codex block, as the runner wires
    one (`opaque_tools=frozenset(...)`, `opaque_tool_args=tuple(...)`)."""
    block = codex_block()
    return _sup(opaque_tools=frozenset(block.get("opaque_tools", []) or []),
                opaque_tool_args=tuple(block.get("opaque_tool_args", []) or []),
                **kw)


def _file_change(path: str = "/w/a.py") -> Event:
    """What codex.py's Normalizer emits for a completed `file_change` item:
    kind=tool, name=<item type>, args={"changes": [{path, kind}]} — path and
    kind only, so every edit of one file hashes identically."""
    return Event(kind="tool", name="file_change",
                 args={"changes": [{"path": path, "kind": "update"}]})


def _run_call(command: str = "pytest") -> Event:
    return Event(kind="tool", name="command_execution", args={"command": command})


def _feed(sup: Supervisor, events) -> list:
    return [t for t in (sup.observe(e) for e in events) if t is not None]


def _reasons(trips) -> list[str]:
    return [t.reason for t in trips]


def test_codex_block_declares_file_change_opaque():
    assert "file_change" in (codex_block().get("opaque_tools") or []), (
        "the codex block must declare file_change opaque, or every repeated "
        "edit to one file trips doom_loop")


def test_file_change_repeated_past_the_threshold_does_not_trip():
    """Six successive edits to one file — six distinct items, one event each —
    against a threshold of 3: a working codex agent mid-edit, no trip."""
    sup = _codex_sup()
    trips = _feed(sup, [_file_change()] * 6)
    assert trips == [], trips


def test_genuinely_repeated_command_execution_still_trips():
    """Positive control: the declaration is scoped to file_change, not a
    general loosening of codex's doom-loop coverage — command_execution's
    args carry the command, so a genuinely repeated call still identifies
    itself and still trips."""
    sup = _codex_sup()
    trips = _feed(sup, [_run_call()] * 3)
    assert _reasons(trips) == ["doom_loop"], trips


def test_file_change_trips_without_the_declaration():
    """Confirms the exemption comes from the codex block's declaration, not
    from anything about the event's name or shape."""
    sup = _sup()  # no opaque_tools configured
    trips = _feed(sup, [_file_change()] * 3)
    assert _reasons(trips) == ["doom_loop"], trips
