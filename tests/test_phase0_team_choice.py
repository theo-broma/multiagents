"""P0-R7 — `init-agent` asks which team, then launches.

Contract: `context/specs/phase0-context-and-team.md`, section P0-R7.

Black box: `init-agent` is driven through `cli.main([...])`, the project is a
real directory with a hand-written `project.yaml`, and the effects are read
back through the file's bytes, `load_config`, stdout/stderr and the return
code. The only things replaced are the ones the contract lets a test replace:

- the launch (`driver._launch_agent`) and the pre-launch checks
  (`_executor_problems`, `_repair_credential_drift`, `driver._orchestrator_hold`),
  so no agent is ever started;
- the keyboard. The contract names `_select(options, current, read_key)` as the
  function the cursor UI is built on, so a scripted terminal is `_select`
  itself, called with a scripted `read_key` instead of the real one. The real
  key reader, `_read_key(stream)` (P0-R7.13), is tested on its own against
  in-memory streams. Whatever the implementation prints —
  from the caller or from inside `_select` — still reaches the output.
"""

from __future__ import annotations

import ast
import difflib
import io
import os
import re
from pathlib import Path

import pytest

import multiagents.cli as cli
from multiagents import driver
from multiagents.config import global_config_dir, load as load_config
from multiagents.config import seed_global
from multiagents.paths import ProjectPaths

ROOT = Path(__file__).resolve().parents[1]
CLI_SOURCE = ROOT / "src" / "multiagents" / "cli.py"
INITIALIZER = ROOT / "src" / "multiagents" / "defaults" / "agents" / "team" / "_initializer.md"

AUDIT3_DESCRIPTION = "Third team, added in the project layer only."
SENTINEL = "<<<the choice was made here>>>"

# A project.yaml written the way people write them: mostly comments, a
# `teams:` key that shares a prefix with `team:`, a commented-out `team:` line,
# and a `team:` key nested under another mapping that must never be touched.
COMMENTED = """\
# Project configuration.
#
# team: review        <- a comment, not a key
executor:
  # why this key exists
  kind: local

notifications:
  team: ops-channel   # nested; not the active team

team: implement

# teams added by this project
teams:
  audit3:
    description: "Third team, added in the project layer only."

limits:
  max_steps: 250
"""

NO_TEAM_LINE = """\
# no team line here: it is inherited from the defaults layer
executor:
  kind: local

limits:
  max_steps: 250
"""


# ------------------------------------------------------------------ helpers --

class _Stdin:
    """A stdin whose tty-ness is chosen and which must never be read from."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty

    def _no(self, *a, **k):
        raise AssertionError("init-agent read from stdin")

    read = readline = readlines = __iter__ = _no

    def fileno(self):
        raise AssertionError("init-agent touched the terminal")


class Harness:
    """Records the order of the observable steps `init-agent` takes."""

    def __init__(self, monkeypatch, tmp_path: Path):
        self.root = tmp_path
        self.paths = ProjectPaths(tmp_path)
        self.paths.config.mkdir(parents=True)
        self.events: list[str] = []
        self.launched: list[object] = []
        self.options_seen: list[list[tuple[str, str]]] = []
        self.current_seen: list[str] = []
        self.keys: list[str] | None = None
        self._mp = monkeypatch

        def launch(paths, config, role, *a, **k):
            self.events.append("launch")
            self.launched.append(config)
            return 0

        def executor_problems(*a, **k):
            self.events.append("executor-check")
            return []

        monkeypatch.setattr(driver, "_launch_agent", launch)
        monkeypatch.setattr(driver, "_orchestrator_hold", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_executor_problems", executor_problems)
        monkeypatch.setattr(cli, "_repair_credential_drift", lambda *a, **k: [])

    @property
    def project_yaml(self) -> Path:
        return self.paths.config / "project.yaml"

    def write(self, text: str) -> None:
        self.project_yaml.write_text(text)
        # Back-date it, so any rewrite at all shows up in st_mtime_ns.
        os.utime(self.project_yaml, ns=(1_000_000_000, 1_000_000_000))

    def snapshot(self) -> tuple[bytes, int]:
        return self.project_yaml.read_bytes(), self.project_yaml.stat().st_mtime_ns

    def tty(self, is_tty: bool) -> None:
        self._mp.setattr(cli.sys, "stdin", _Stdin(is_tty))

    def script_keys(self, keys: list[str]) -> None:
        """Answer the prompt with `keys`, through the real `_select`."""
        real = getattr(cli, "_select", None)
        self.keys = list(keys)

        def scripted(options, current, read_key):
            self.events.append("select")
            self.options_seen.append(list(options))
            self.current_seen.append(current)
            queue = iter(self.keys)

            def read():
                try:
                    return next(queue)
                except StopIteration:
                    raise AssertionError("_select read past the end of the script")

            result = real(options, current, read)
            print(SENTINEL)
            return result

        self._mp.setattr(cli, "_select", scripted, raising=False)

    def forbid_prompt(self) -> None:
        def never(*a, **k):
            self.events.append("select")
            raise AssertionError("the team list was offered")

        self._mp.setattr(cli, "_select", never, raising=False)

    def run(self, *extra: str) -> int:
        return cli.main(["--path", str(self.root), "init-agent", *extra])


@pytest.fixture
def h(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _after(out: str, marker: str = SENTINEL) -> str:
    assert marker in out, "the scripted choice was never offered"
    return out.split(marker, 1)[1]


def _team_names(text: str) -> list[str]:
    """The team named by each printed team line: `team`, whitespace, the name
    (amendment 2026-09-23); what follows the name is free."""
    return [m.group(1) for line in text.splitlines()
            if (m := re.match(r"^team\s+([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])", line))]


def _one_line_diff(before: str, after: str) -> list[str]:
    return [line for line in difflib.ndiff(before.splitlines(), after.splitlines())
            if line[:1] in "+-"]


# ------------------------------------------------------------------- P0-R7.1 --

def test_p0_r7_1_the_list_comes_before_the_checks_and_the_launch(h, capsys):
    h.write(COMMENTED)
    h.tty(True)
    h.script_keys(["enter"])
    teams = load_config(h.paths).teams
    assert len(teams) >= 2

    assert h.run() == 0

    out = capsys.readouterr()
    shown = _norm(out.out + out.err)
    for name, spec in teams.items():
        assert name in shown, f"team {name!r} was not offered"
        assert _norm(str(spec.get("description", ""))) in shown, \
            f"team {name!r} was offered without its description"
    assert h.events[0] == "select", h.events
    assert h.events.index("select") < h.events.index("executor-check")
    assert h.events.count("launch") == 1 and h.events[-1] == "launch"


def test_p0_r7_1_the_configured_team_is_selected_on_entry(h):
    h.write(COMMENTED.replace("team: implement", "team: review"))
    h.tty(True)
    h.script_keys(["enter"])

    assert h.run() == 0
    assert h.current_seen == ["review"]
    assert load_config(h.paths).team == "review"


# ------------------------------------------------------------------- P0-R7.2 --

def test_p0_r7_2_options_are_the_config_teams_in_order(h):
    h.write(COMMENTED)
    h.tty(True)
    h.script_keys(["enter"])
    teams = load_config(h.paths).teams

    assert h.run() == 0
    assert h.options_seen, "the team list was never offered"
    assert [name for name, _ in h.options_seen[0]] == list(teams)
    assert "audit3" in [name for name, _ in h.options_seen[0]]


def test_p0_r7_2_a_team_added_in_the_project_layer_is_selectable(h, capsys):
    h.write(COMMENTED)
    h.tty(True)
    order = list(load_config(h.paths).teams)
    steps = order.index("audit3") - order.index("implement")
    h.script_keys(["down"] * steps + ["enter"])

    assert h.run() == 0
    assert AUDIT3_DESCRIPTION in _norm(capsys.readouterr().out)
    assert load_config(h.paths).team == "audit3"
    assert len(h.launched) == 1


def test_p0_r7_2_no_team_name_literal_in_cli():
    tree = ast.parse(CLI_SOURCE.read_text())
    literals = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value in ("implement", "review")]
    assert literals == [], f"team names hardcoded in cli.py at lines {literals}"


# ------------------------------------------------------------------- P0-R7.3 --

OPTS = [("a", "first"), ("b", "second"), ("c", "third")]


def _keys(*tokens):
    queue = iter(tokens)

    def read():
        try:
            return next(queue)
        except StopIteration:
            raise AssertionError("_select read past the end of the script")
    return read


def test_p0_r7_3_enter_on_entry_returns_current():
    assert cli._select(OPTS, "b", _keys("enter")) == "b"


def test_p0_r7_3_down_then_enter_returns_the_next():
    assert cli._select(OPTS, "a", _keys("down", "enter")) == "b"


def test_p0_r7_3_up_then_enter_returns_the_previous():
    assert cli._select(OPTS, "c", _keys("up", "enter")) == "b"


def test_p0_r7_3_up_at_the_top_stays_at_the_top():
    assert cli._select(OPTS, "a", _keys("up", "up", "enter")) == "a"
    # No wrap-around: one step back down from the clamped top is the second.
    assert cli._select(OPTS, "a", _keys("up", "down", "enter")) == "b"


def test_p0_r7_3_down_past_the_end_stays_at_the_end():
    assert cli._select(OPTS, "b", _keys("down", "down", "down", "enter")) == "c"
    assert cli._select(OPTS, "c", _keys("down", "up", "enter")) == "b"


def test_p0_r7_3_cancel_returns_none():
    assert cli._select(OPTS, "a", _keys("cancel")) is None
    assert cli._select(OPTS, "a", _keys("down", "down", "cancel")) is None


def test_p0_r7_3_unknown_tokens_are_ignored():
    assert cli._select(OPTS, "a", _keys("x", "", "left", "ENTER", "q", "down",
                                        "\x1b", "enter")) == "b"


def test_p0_r7_3_current_not_among_options_selects_the_first():
    assert cli._select(OPTS, "zzz", _keys("enter")) == "a"
    assert cli._select(OPTS, "", _keys("down", "enter")) == "b"


def test_p0_r7_3_a_single_option_is_clamped_both_ways():
    assert cli._select([("only", "x")], "only", _keys("down", "up", "down", "enter")) == "only"


def test_p0_r7_3_no_options_returns_none_without_reading():
    def read():
        raise AssertionError("_select read a key with nothing to choose")
    assert cli._select([], "implement", read) is None
    assert cli._select([], "", read) is None


# ------------------------------------------------------------------- P0-R7.4 --

def test_p0_r7_4_cancel_changes_nothing(h, capsys):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.script_keys(["down", "cancel"])

    assert h.run() == 130
    assert h.snapshot() == before
    assert h.launched == []
    said = [line for line in _after(capsys.readouterr().out).splitlines() if line.strip()]
    assert len(said) == 1, said
    assert "nothing" in said[0].lower() and "changed" in said[0].lower(), said[0]
    # The choice comes first, so cancelling skips everything after it.
    assert "executor-check" not in h.events, h.events


# ------------------------------------------------------------------- P0-R7.5 --

def test_p0_r7_5_choosing_the_current_team_writes_nothing(h):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.script_keys(["down", "up", "enter"])     # moves away and back

    assert h.run() == 0
    assert "select" in h.events, "the team list was never offered"
    assert h.snapshot() == before
    assert len(h.launched) == 1


# ------------------------------------------------------------------- P0-R7.6 --

def test_p0_r7_6_set_team_rewrites_one_line(h):
    h.write(COMMENTED)

    assert cli._set_team(h.paths, "review") is True

    after = h.project_yaml.read_text()
    diff = _one_line_diff(COMMENTED, after)
    assert len(diff) == 2, diff
    assert diff[0] == "- team: implement"
    assert re.fullmatch(r"\+ team:\s*review", diff[1]), diff
    config = load_config(h.paths)
    assert config.team == "review"
    assert config.project["notifications"]["team"] == "ops-channel"
    assert "audit3" in config.teams


def test_p0_r7_6_set_team_never_touches_a_nested_team_key(h):
    # The nested key comes first, so a first-match edit would hit it.
    h.write("block:\n  team: nested\nteam: implement\n")

    assert cli._set_team(h.paths, "review") is True
    assert h.project_yaml.read_text() == "block:\n  team: nested\nteam: review\n"


def test_p0_r7_6_an_inline_comment_on_the_team_line_is_kept(h):
    before = COMMENTED.replace("team: implement\n", "team: implement  # why\n")
    h.write(before)

    assert cli._set_team(h.paths, "review") is True

    diff = _one_line_diff(before, h.project_yaml.read_text())
    assert len(diff) == 2 and diff[0] == "- team: implement  # why", diff
    assert re.fullmatch(r"\+ team:\s*review\s+# why", diff[1]), diff
    assert load_config(h.paths).team == "review"


@pytest.mark.parametrize("quoted", ['"implement"', "'implement'"])
def test_p0_r7_6_a_quoted_value_becomes_the_bare_name(h, quoted):
    before = COMMENTED.replace("team: implement\n", f"team: {quoted}\n")
    h.write(before)

    assert cli._set_team(h.paths, "review") is True

    diff = _one_line_diff(before, h.project_yaml.read_text())
    assert diff == [f"- team: {quoted}", "+ team: review"], diff
    assert load_config(h.paths).team == "review"


def test_p0_r7_6_set_team_is_idempotent(h):
    h.write(COMMENTED)
    assert cli._set_team(h.paths, "review") is True
    once = h.project_yaml.read_bytes()
    assert cli._set_team(h.paths, "review") is True
    assert h.project_yaml.read_bytes() == once


def test_p0_r7_6_set_team_has_a_docstring_saying_why():
    doc = (cli._set_team.__doc__ or "").lower()
    assert "comment" in doc, "the docstring should say why it is a line edit"


def test_p0_r7_6_choosing_another_team_in_init_agent_writes_one_line(h, capsys):
    h.write(COMMENTED)
    h.tty(True)
    order = list(load_config(h.paths).teams)
    h.script_keys(["down"] * (order.index("review") - order.index("implement")) + ["enter"])

    assert h.run() == 0

    diff = _one_line_diff(COMMENTED, h.project_yaml.read_text())
    assert len(diff) == 2 and diff[0] == "- team: implement", diff
    assert load_config(h.paths).team == "review"
    assert len(h.launched) == 1
    # R7.11 rides on this test: what is printed about the team afterwards
    # names the team just chosen.
    names = _team_names(_after(capsys.readouterr().out))
    assert names, "nothing was printed about the team after the choice"
    assert names[-1] == "review", names


# ------------------------------------------------------------------- P0-R7.7 --

def _assert_only_inserted(before: str, after: str, team: str) -> None:
    diff = _one_line_diff(before, after)
    assert all(line.startswith("+ ") or line == "+" for line in diff), \
        f"lines other than the inserted one changed: {diff}"
    added = [line[2:] for line in diff]
    content = [line for line in added if line.strip()]
    assert len(content) == 1 and re.fullmatch(rf"team:\s*{team}", content[0]), added
    assert len(added) - len(content) <= 1, f"more than one blank line added: {added}"


def test_p0_r7_7_a_missing_team_line_is_inserted(h):
    h.write(NO_TEAM_LINE)

    assert cli._set_team(h.paths, "review") is True

    _assert_only_inserted(NO_TEAM_LINE, h.project_yaml.read_text(), "review")
    config = load_config(h.paths)
    assert config.team == "review"
    assert config.project["limits"]["max_steps"] == 250


def test_p0_r7_7_inserted_when_the_last_line_has_no_newline(h):
    # Ends inside a nested mapping with no trailing newline: an append without
    # a newline would glue onto `max_steps`, an indented one would nest.
    text = NO_TEAM_LINE.rstrip("\n")
    h.write(text)

    assert cli._set_team(h.paths, "review") is True

    _assert_only_inserted(text, h.project_yaml.read_text(), "review")
    config = load_config(h.paths)
    assert config.team == "review"
    assert config.project["limits"]["max_steps"] == 250


def test_p0_r7_7_no_project_yaml_returns_false(h):
    assert not h.project_yaml.exists()
    assert cli._set_team(h.paths, "review") is False
    assert not h.project_yaml.exists()


def test_p0_r7_7_init_agent_with_no_project_yaml_refuses_to_launch(h, capsys):
    h.tty(True)
    order = list(load_config(h.paths).teams)
    current = load_config(h.paths).team
    other = next(name for name in order if name != current)
    h.script_keys(["down"] * (order.index(other) - order.index(current)) + ["enter"])

    assert h.run() == 2
    assert h.launched == []
    assert not h.project_yaml.exists()
    out = capsys.readouterr()
    assert "project.yaml" in _after(out.out) + out.err, "it did not say why"


# ------------------------------------------------------------------- P0-R7.8 --

def test_p0_r7_8_no_terminal_no_prompt(h, capsys):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(False)
    h.forbid_prompt()

    assert h.run() == 0

    assert "select" not in h.events
    assert h.snapshot() == before
    assert len(h.launched) == 1
    assert _team_names(capsys.readouterr().out) == ["implement"]


# ------------------------------------------------------------------- P0-R7.9 --

def test_p0_r7_9_team_flag_same_as_current_writes_nothing(h):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()

    assert h.run("--team", "implement") == 0
    assert "select" not in h.events
    assert h.snapshot() == before
    assert len(h.launched) == 1


def test_p0_r7_9_team_flag_different_writes_it_then_launches(h, capsys):
    h.write(COMMENTED)
    h.tty(True)
    h.forbid_prompt()

    assert h.run("--team", "audit3") == 0
    assert "select" not in h.events
    diff = _one_line_diff(COMMENTED, h.project_yaml.read_text())
    assert len(diff) == 2 and diff[0] == "- team: implement", diff
    assert load_config(h.paths).team == "audit3"
    assert len(h.launched) == 1
    names = _team_names(capsys.readouterr().out)
    assert names and names[-1] == "audit3", names


def test_p0_r7_9_unknown_team_is_refused(h, capsys):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()
    teams = list(load_config(h.paths).teams)

    assert h.run("--team", "nosuchteam") == 2

    assert "select" not in h.events
    assert h.snapshot() == before
    assert h.launched == []
    err = capsys.readouterr().err
    for name in teams:
        assert name in err, f"{name!r} missing from the list printed to stderr"


def test_p0_r7_9_team_flag_applies_without_a_terminal(h, capsys):
    # Amendment 2026-09-23: `--team` is a deliberate choice, tty or not.
    h.write(COMMENTED)
    h.tty(False)
    h.forbid_prompt()

    assert h.run("--team", "review") == 0

    assert "select" not in h.events
    diff = _one_line_diff(COMMENTED, h.project_yaml.read_text())
    assert diff == ["- team: implement", "+ team: review"], diff
    assert load_config(h.paths).team == "review"
    assert len(h.launched) == 1
    names = _team_names(capsys.readouterr().out)
    assert names and names[-1] == "review", names


def test_p0_r7_9_empty_team_flag_is_unknown(h, capsys):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()
    teams = list(load_config(h.paths).teams)

    assert h.run("--team", "") == 2

    assert "select" not in h.events
    assert h.snapshot() == before
    assert h.launched == []
    err = capsys.readouterr().err
    for name in teams:
        assert name in err, f"{name!r} missing from the list printed to stderr"


def test_p0_r7_9_team_flag_is_case_sensitive(h, capsys):
    h.write(COMMENTED)
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()

    assert h.run("--team", "Implement") == 2
    assert h.snapshot() == before
    assert h.launched == []


# ------------------------------------------------------------------ P0-R7.10 --

def _single_team(h):
    """One team configured: the global layer clears `teams`, the project adds one."""
    seed_global()
    layer = global_config_dir() / "project.yaml"
    layer.write_text(layer.read_text() + "\nteams: null\n")
    h.write("team: solo\nteams:\n  solo:\n    description: the only one\n")
    assert list(load_config(h.paths).teams) == ["solo"]


def test_p0_r7_10_one_team_no_prompt(h, capsys):
    _single_team(h)
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()

    assert h.run() == 0

    assert "select" not in h.events
    assert h.snapshot() == before
    assert len(h.launched) == 1
    assert _team_names(capsys.readouterr().out) == ["solo"]


def test_p0_r7_10_zero_teams_no_prompt(h, capsys):
    h.write("team: implement\nteams: null\n")
    assert load_config(h.paths).teams == {}
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()

    assert h.run() == 0

    assert "select" not in h.events
    assert h.snapshot() == before
    assert len(h.launched) == 1
    assert _team_names(capsys.readouterr().out) == ["implement"]


def test_p0_r7_10_team_flag_with_empty_teams_is_unknown(h):
    h.write("team: implement\nteams: null\n")
    before = h.snapshot()
    h.tty(True)
    h.forbid_prompt()

    assert h.run("--team", "implement") == 2
    assert h.snapshot() == before
    assert h.launched == []


# ------------------------------------------------------------------ P0-R7.11 --

def test_p0_r7_11_the_team_printed_after_the_choice_is_the_new_one(h, capsys):
    h.write(COMMENTED)
    h.tty(True)
    order = list(load_config(h.paths).teams)
    h.script_keys(["down"] * (order.index("audit3") - order.index("implement")) + ["enter"])

    assert h.run() == 0
    names = _team_names(_after(capsys.readouterr().out))
    assert names, "nothing was printed about the team after the choice"
    assert all(name == "audit3" for name in names), names


# ------------------------------------------------------------------ P0-R7.12 --

def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def test_p0_r7_12_the_initializer_brief_names_the_route_and_the_next_phase():
    paragraphs = [p for p in _paragraphs(INITIALIZER.read_text())
                  if "init-agent --team" in p]
    assert paragraphs, "_initializer.md never mentions `init-agent --team`"
    assert any(re.search(r"\bnext\b", p, re.I) for p in paragraphs), \
        "the team paragraph does not say the proposal is for the next phase"
    assert any(re.search(r"\buser\b", p, re.I) for p in paragraphs), \
        "the team paragraph does not put the choice to the user"


# ------------------------------------------------------------------ P0-R7.13 --

KEYS = [
    ("\x1b[A", "up"), ("k", "up"),
    ("\x1b[B", "down"), ("j", "down"),
    ("\r", "enter"), ("\n", "enter"),
    ("q", "cancel"), ("\x03", "cancel"),
    ("\x1bx", "cancel"),           # ESC not followed by `[`
    ("\x1b", "cancel"),            # ESC, then end of stream
    ("", "cancel"),                # end of stream, nothing read
    ("x", "x"), ("Z", "Z"), (" ", " "),
]


def _stream(kind: str, text: str):
    return io.BytesIO(text.encode()) if kind == "bytes" else io.StringIO(text)


@pytest.mark.parametrize("kind", ["bytes", "text"])
@pytest.mark.parametrize("raw,token", KEYS, ids=[repr(r) for r, _ in KEYS])
def test_p0_r7_13_read_key_maps_each_key(kind, raw, token):
    assert cli._read_key(_stream(kind, raw)) == token


@pytest.mark.parametrize("kind", ["bytes", "text"])
def test_p0_r7_13_read_key_reads_one_key_at_a_time(kind):
    stream = _stream(kind, "\x1b[Aj\x1b[Bk\rq")
    assert [cli._read_key(stream) for _ in range(6)] == \
        ["up", "down", "down", "up", "enter", "cancel"]
    assert cli._read_key(stream) == "cancel", "end of stream after the keys"


@pytest.mark.parametrize("kind", ["bytes", "text"])
def test_p0_r7_13_read_key_drives_select(kind):
    stream = _stream(kind, "jj\x1b[A\r")
    assert cli._select(OPTS, "a", lambda: cli._read_key(stream)) == "b"


@pytest.mark.parametrize("kind", ["bytes", "text"])
def test_p0_r7_13_an_exhausted_stream_cancels_select(kind):
    stream = _stream(kind, "j")
    assert cli._select(OPTS, "a", lambda: cli._read_key(stream)) is None


# ------------------------------------------------------------------ P0-R7.14 --

def test_p0_r7_14_the_launch_sees_the_team_chosen_at_the_prompt(h):
    h.write(COMMENTED)
    h.tty(True)
    order = list(load_config(h.paths).teams)
    h.script_keys(["down"] * (order.index("audit3") - order.index("implement")) + ["enter"])

    assert h.run() == 0
    assert len(h.launched) == 1 and h.launched[0].team == "audit3"


def test_p0_r7_14_the_launch_sees_the_team_given_by_flag(h):
    h.write(COMMENTED)
    h.tty(True)
    h.forbid_prompt()

    assert h.run("--team", "review") == 0
    assert len(h.launched) == 1 and h.launched[0].team == "review"


def test_p0_r7_14_the_launch_sees_an_inserted_team(h):
    h.write(NO_TEAM_LINE)
    h.tty(False)
    h.forbid_prompt()

    assert h.run("--team", "review") == 0
    assert len(h.launched) == 1 and h.launched[0].team == "review"
    assert load_config(h.paths).team == "review"
