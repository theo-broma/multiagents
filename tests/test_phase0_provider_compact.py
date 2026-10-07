"""Phase 0, contract B, group G — P0-R8d and P0-R8e: the provider scripts'
`compact` action, and the automatic compaction threshold as a per-agent key.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8d, § P0-R8e.

The shipped scripts under `src/multiagents/defaults/providers/` are run as the
driver runs them — `sh <script> <action>` — with a fake CLI binary first on
PATH (and named by `MULTIAGENTS_BIN`), `HOME` pointed at a scratch directory,
and the cwd a scratch project. The fake records its argv and cwd, and does to
the session transcript under `$HOME/.claude/projects/<slug>/<sid>.jsonl` what
the test asks: append a manual compaction record, append nothing, append an
automatic one, or fail. What is asserted is the script's exit code, its stdout
and stderr, and what the fake saw.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
import p0_context_harness as ch  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402

SID = "c0de0000-0000-4000-8000-00000000cafe"
SCRIPTS = ch.PROVIDER_SCRIPTS
SHIPPED = ("claude", "agy", "opencode")

FAKE_CLI = r'''#!{python}
"""A stand-in provider CLI. Logs what it was asked, then does FAKE_MODE."""
import json, os, pathlib, sys
log = os.environ.get("FAKE_LOG") or "{log}"
# C3: a spawned run gets its prompt on stdin; the compact launcher passes it in argv.
prompt = "" if "/compact" in sys.argv[1:] else sys.stdin.read()
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt,
                             "cwd": os.getcwd()}) + "\n")
mode = os.environ.get("FAKE_MODE", "manual")
argv = sys.argv[1:]
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else ""
slug = os.getcwd().replace("/", "-").replace(".", "-").replace("_", "-")
transcript = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / slug / f"{sid}.jsonl"
record = {"type": "system", "subtype": "compact_boundary", "sessionId": sid,
          "content": "Conversation compacted",
          "compactMetadata": {"trigger": "manual", "preTokens": 27729,
                              "postTokens": 1607, "durationMs": 23771,
                              "cumulativeDroppedTokens": 26122}}
if mode == "fail":
    sys.stderr.write("API Error: 529 overloaded\n")
    sys.exit(3)
if mode in ("manual", "auto", "same") and sid and transcript.parent.is_dir():
    if mode == "auto":
        record["compactMetadata"]["trigger"] = "auto"
    with transcript.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "", "session_id": sid}))
sys.exit(0)
'''


class Scratch:
    """HOME, a project cwd, a fake CLI on PATH, and the session transcript."""

    def __init__(self, tmp_path: Path, name: str = "claude"):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.cwd = (tmp_path / "work_dir.proj").resolve()   # '_' and '.' in the slug
        self.cwd.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.fake = self.bin / name
        self.log = tmp_path / "fake-cli.log"
        # The log path is baked in as well: a spawned agent's environment is
        # built from a clean slate, so FAKE_LOG does not reach it.
        self.fake.write_text(FAKE_CLI.replace("{python}", sys.executable)
                             .replace("{log}", str(self.log)))
        self.fake.chmod(0o755)
        self.transcript = (self.home / ".claude" / "projects" / ch.slug(self.cwd)
                           / f"{SID}.jsonl")

    def session(self, records: list[dict] | None = None) -> None:
        ch.write_transcript(self.transcript, records if records is not None
                            else [ch.user("hi"), ch.request(27_729)])

    def run(self, script: str, action: str, *, mode: str = "manual",
            sid: str | None = SID, **env: str) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                "MULTIAGENTS_BIN": str(self.fake), "FAKE_LOG": str(self.log),
                "FAKE_MODE": mode, "MULTIAGENTS_MODEL": "m",
                "MULTIAGENTS_PROVIDER": script,
                "MULTIAGENTS_LAUNCH_STATE": str(self.home)}
        if sid is not None:
            base["MULTIAGENTS_SESSION_ID"] = sid
        return subprocess.run(["sh", str(SCRIPTS / f"{script}.sh"), action],
                              capture_output=True, text=True, cwd=self.cwd,
                              env={**base, **env}, timeout=60)

    def calls(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]


@pytest.fixture
def scratch(tmp_path):
    return Scratch(tmp_path)


# ------------------------------------------------------------ P0-R8d.1 --

def test_p0_r8d_1_the_readme_documents_compact():
    text = (SCRIPTS / "README.md").read_text()
    block = re.search(r"<provider>\.sh compact.*?(?=\n\s*<provider>\.sh |\n## |\Z)",
                      text, flags=re.S)
    assert block, "README.md has no `<provider>.sh compact` entry in its contract"
    entry = block.group(0)
    assert "MULTIAGENTS_SESSION_ID" in entry
    assert re.search(r"exit 0\b", entry) and re.search(r"exit 64\b", entry)
    assert "stderr" in entry
    # Documented beside the others, in the Contract section.
    contract = text[text.index("## Contract"):]
    contract = contract[:contract.index("\n## ", 1)] if "\n## " in contract[1:] else contract
    assert "<provider>.sh compact" in contract


@pytest.mark.parametrize("name", SHIPPED)
def test_p0_r8d_1_each_usage_line_lists_compact(scratch, name):
    got = scratch.run(name, "no-such-action")
    assert got.returncode == 64
    usage = [line for line in got.stderr.splitlines() if "usage:" in line]
    assert usage, f"{name}.sh printed no usage line: {got.stderr!r}"
    actions = re.split(r"[|\s]+", usage[-1].split("usage:", 1)[1])
    assert "compact" in actions, f"{name}.sh usage does not list compact: {usage[-1]}"


# ------------------------------------------------------------ P0-R8d.2 --

def test_p0_r8d_2_a_manual_compaction_appended_is_success(scratch):
    scratch.session()
    got = scratch.run("claude", "compact", mode="manual")
    assert got.returncode == 0, got.stderr
    first = got.stdout.splitlines()[0] if got.stdout else ""
    assert first == "27729 -> 1607 tokens"


def test_p0_r8d_2_runs_the_cli_headless_against_the_session_in_the_launch_dir(scratch):
    scratch.session()
    scratch.run("claude", "compact", mode="manual")
    calls = scratch.calls()
    assert len(calls) == 1, f"the CLI was run {len(calls)} times"
    argv = calls[0]["argv"]
    assert "-p" in argv and "/compact" in argv
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == SID
    assert Path(calls[0]["cwd"]).resolve() == scratch.cwd


def test_p0_r8d_2_the_cli_exiting_0_with_nothing_recorded_is_failure(scratch):
    scratch.session()
    got = scratch.run("claude", "compact", mode="nothing")
    assert scratch.calls(), "the CLI was never run"
    assert got.returncode == 1
    assert "compact" in got.stderr.lower() and "record" in got.stderr.lower(), (
        f"stderr should say no compaction was recorded: {got.stderr!r}")


def test_p0_r8d_2_a_record_from_before_the_call_does_not_count(scratch):
    scratch.session([ch.user("hi"), ch.request(40_000), ch.compaction(40_000, 2_000),
                     ch.request(2_500)])
    got = scratch.run("claude", "compact", mode="nothing")
    assert scratch.calls(), "the CLI was never run"
    assert got.returncode == 1


def test_p0_r8d_2_an_identical_record_appended_by_this_call_counts(scratch):
    """"appended by this call" — judged by what is new, not by whether an equal
    record exists: the earlier record here has the same figures."""
    scratch.session([ch.user("hi"), ch.request(27_729), ch.compaction(27_729, 1_607),
                     ch.request(1_700)])
    got = scratch.run("claude", "compact", mode="same")
    assert got.returncode == 0, got.stderr
    assert got.stdout.splitlines()[0] == "27729 -> 1607 tokens"


def test_p0_r8d_2_an_automatic_compaction_is_not_this_calls(scratch):
    scratch.session()
    got = scratch.run("claude", "compact", mode="auto")
    assert got.returncode == 1


@pytest.mark.parametrize("sid", [None, ""])
def test_p0_r8d_2_no_session_id_is_exit_2(scratch, sid):
    scratch.session()
    got = scratch.run("claude", "compact", sid=sid)
    assert got.returncode == 2
    assert got.stderr.strip(), "the reason goes to stderr"
    assert scratch.calls() == [], "the CLI ran without a session to compact"


def test_p0_r8d_2_no_transcript_for_the_session_is_exit_1(scratch):
    got = scratch.run("claude", "compact")
    assert got.returncode == 1
    assert got.stderr.strip()


def test_p0_r8d_2_a_transcript_under_another_directory_is_not_this_session(scratch):
    other = scratch.home / ".claude" / "projects" / ch.slug(scratch.cwd.parent) / f"{SID}.jsonl"
    ch.write_transcript(other, [ch.request(27_729)])
    got = scratch.run("claude", "compact")
    assert got.returncode == 1


def test_p0_r8d_2_the_cli_failing_is_failure(scratch):
    scratch.session()
    got = scratch.run("claude", "compact", mode="fail")
    assert scratch.calls(), "the CLI was never run"
    assert got.returncode != 0
    assert got.returncode != 64, "64 means 'cannot compact', not 'tried and failed'"


# ------------------------------------------------- P0-R8d.3 / P0-R8d.4 --

def test_p0_r8d_3_agy_exits_64_without_starting_the_cli(tmp_path):
    sc = Scratch(tmp_path, name="agy")
    sc.session()
    got = sc.run("agy", "compact")
    assert got.returncode == 64
    assert sc.calls() == [], "agy.sh compact started the CLI"
    text = (SCRIPTS / "agy.sh").read_text()
    assert "42,752" in text or "42752" in text, "the reason (42,752 tokens) is recorded"


def test_p0_r8d_4_opencode_exits_64_without_starting_the_cli(tmp_path):
    sc = Scratch(tmp_path, name="opencode")
    sc.session()
    got = sc.run("opencode", "compact")
    assert got.returncode == 64
    assert sc.calls() == [], "opencode.sh compact started the CLI"
    text = (SCRIPTS / "opencode.sh").read_text()
    assert "/summarize" in text, "the route is named where it would be wired"


# ------------------------------------------------------------ P0-R8e.1 --

def _shipped_providers(tmp_path):
    paths = ProjectPaths(tmp_path / "p")
    paths.ensure()
    return load_providers(config_mod.load(paths).providers)


def _spawn_argv(provider, **options) -> list[str]:
    return provider.build_command(prompt="do it", model="m", workdir="/w",
                                  options={"effort": None, **options}, timeout=60)


@pytest.mark.parametrize("value", ["400000", 400000, "auto"])
def test_p0_r8e_1_claude_spawn_with_the_key_gets_the_flag(tmp_path, value):
    argv = _spawn_argv(_shipped_providers(tmp_path)["claude"], autocompact=value)
    assert "--autocompact" in argv, argv
    assert argv[argv.index("--autocompact") + 1] == str(value)


def test_p0_r8e_1_claude_spawn_without_the_key_has_no_flag(tmp_path):
    argv = _spawn_argv(_shipped_providers(tmp_path)["claude"])
    assert "--autocompact" not in argv


@pytest.mark.parametrize("name", ["opencode", "agy"])
def test_p0_r8e_1_providers_without_the_option_ignore_the_key(tmp_path, name):
    providers = _shipped_providers(tmp_path)
    if name not in providers:
        pytest.skip(f"{name} is not shipped")
    argv = _spawn_argv(providers[name], autocompact="400000")
    assert not any("autocompact" in a for a in argv), argv
    assert "400000" not in argv


def test_p0_r8e_1_the_key_reaches_argv_from_agents_yaml(tmp_path, monkeypatch):
    """From the roster, not just the options map: an agent entry carrying
    `autocompact:` spawns with the flag, one without does not. Observed on
    the spawned process's own argv, through the fake CLI's log."""
    from multiagents import server

    root = h.make_git_repo((tmp_path / "proj").resolve())
    cfg = root / ".multiagents" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    sc = Scratch(tmp_path, name="claude")
    shipped = yaml.safe_load((ch.SHIPPED / "providers.yaml").read_text())["providers"]["claude"]
    block = {k: v for k, v in shipped.items() if k in ("spawn", "stream", "transcript")}
    block["bin"] = str(sc.fake)
    block["env"] = {"FAKE_LOG": str(sc.log), "FAKE_MODE": "nothing"}   # EV-R2
    (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": {"fakeclaude": block}}))
    # No team: the roster check is not under test, the argv is.
    (cfg / "project.yaml").write_text(yaml.safe_dump({"team": ""}))
    (cfg / "agents.yaml").write_text(yaml.safe_dump({"agents": {
        "withkey": {"provider": "fakeclaude", "model": "m", "autocompact": 400000,
                    "description": "x", "instructions": ""},
        "nokey": {"provider": "fakeclaude", "model": "m",
                  "description": "x", "instructions": ""},
    }}))
    h.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.setenv("FAKE_LOG", str(sc.log))
    monkeypatch.setenv("FAKE_MODE", "nothing")
    monkeypatch.chdir(root)
    server._reset()

    async def go():
        for agent in ("withkey", "nokey"):
            result = await server.start_agent(agent, f"t {agent}")
            assert "error" not in result, result
        # The CLI logs its argv as it starts; how the run ends is not under test.
        for _ in range(150):
            if len(sc.calls()) >= 2:
                break
            await asyncio.sleep(0.1)

    try:
        asyncio.run(go())
    finally:
        server._reset()
    by_task = {}
    for call in sc.calls():
        argv = call["argv"]
        by_task["withkey" if "t withkey" in call["prompt"] else "nokey"] = argv
    assert set(by_task) == {"withkey", "nokey"}, sc.calls()
    assert "--autocompact" in by_task["withkey"]
    assert by_task["withkey"][by_task["withkey"].index("--autocompact") + 1] == "400000"
    assert "--autocompact" not in by_task["nokey"]


def _launch(scratch: Scratch, **env) -> list[str]:
    got = scratch.run("claude", "launch", MULTIAGENTS_BIN="/bin/echo", **env)
    assert got.returncode == 0, got.stderr
    return got.stdout.split()


@pytest.mark.parametrize("unattended", ["0", "1"])
def test_p0_r8e_1_claude_launch_passes_autocompact_from_the_env(scratch, unattended):
    argv = _launch(scratch, MULTIAGENTS_AUTOCOMPACT="400000",
                   MULTIAGENTS_UNATTENDED=unattended, MULTIAGENTS_NUDGE="go")
    assert "--autocompact" in argv, argv
    assert argv[argv.index("--autocompact") + 1] == "400000"


@pytest.mark.parametrize("value", [None, ""])
def test_p0_r8e_1_claude_launch_without_it_has_no_flag(scratch, value):
    env = {} if value is None else {"MULTIAGENTS_AUTOCOMPACT": value}
    argv = _launch(scratch, **env)
    assert "--autocompact" not in argv


def _launch_env(tmp_path, monkeypatch, entry_extra: dict) -> dict:
    """Launch the orchestrator unattended for one turn through the real
    `_launch_agent`, and return the environment its launch action saw."""
    from multiagents import driver

    root = h.make_git_repo((tmp_path / "proj").resolve())
    paths = ProjectPaths(root)
    paths.ensure()
    fake = ch.FakeProvider(paths.config, tmp_path)
    fake.control(turns=[{"exit": 0}])
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "fakeprov": {"bin": "true", "script": fake.name, "spawn": {"args": ["x"]},
                     "env": fake.env}}}))
    role = {"provider": "fakeprov", "model": "m", "launch": True, **entry_extra}
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {
        "orchestrator": {**role, "role": "orchestrator"}}}))
    monkeypatch.setenv("FAKE_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_CTL", str(fake.ctl))
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.chdir(root)
    config = config_mod.load(paths)
    driver._launch_agent(paths, config, "orchestrator", resume=False, unattended=1)
    launches = fake.calls("launch")
    assert launches, f"the launch action never ran: {fake.calls()}"
    return launches[0]["env"]


def test_p0_r8e_1_a_launched_role_with_the_key_hands_it_to_launch(tmp_path, monkeypatch):
    env = _launch_env(tmp_path, monkeypatch, {"autocompact": 400000})
    assert env.get("MULTIAGENTS_AUTOCOMPACT") == "400000"


def test_p0_r8e_1_a_launched_role_without_the_key_hands_nothing(tmp_path, monkeypatch):
    env = _launch_env(tmp_path, monkeypatch, {})
    assert not env.get("MULTIAGENTS_AUTOCOMPACT")


# ------------------------------------------------------------ P0-R8e.2 --

def test_p0_r8e_2_no_shipped_agent_carries_autocompact():
    agents = yaml.safe_load((ch.SHIPPED / "agents.yaml").read_text())["agents"]
    carrying = [name for name, entry in agents.items()
                if isinstance(entry, dict) and "autocompact" in entry]
    assert carrying == []
