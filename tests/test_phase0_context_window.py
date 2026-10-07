"""Phase 0, contract B, group F — P0-R8a and P0-R8b: the orchestrator is told
when its own context window is filling, and its brief says what a compaction
loses.

Contract: `context/specs/phase0-context-and-team.md` § P0-R8a, § P0-R8b.

Black box throughout:

- `session_context(provider, cwd, session_id)` is called by its contract
  signature. Its module is the developer's choice, so it is located by
  searching every module of the package (`p0_context_harness.
  find_session_context`); until it exists, each test using it fails with
  "session_context is not defined in any multiagents module".
- The notice is observed on the MCP server's tool responses, called the way the
  MCP host calls them — the same approach `test_phase0_config_reload.py` uses
  for `config_reload` — and in the project's events log.
- "No read" and "not re-read" are observed at the filesystem boundary: a
  counter over `open`/`io.open`/`os.open` that sees only whether bytes of the
  transcript file were read, not how.

The provider in these tests is called `txp`, not `claude`: the reading must be
provider-agnostic, and the transcript block is the only thing it may go on.
"""

from __future__ import annotations

import os
import re
import statistics
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
import p0_context_harness as ch  # noqa: E402

from multiagents import config as config_mod  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import Provider  # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now  # noqa: E402

SID = "5e551011-0000-4000-8000-00000000abcd"
WIND_DOWN = 10_000
COMPACT_AT = 8_000


def _session_context():
    fn = ch.find_session_context()
    if fn is None:
        pytest.fail("session_context is not defined in any multiagents module "
                    "(P0-R8a.2 names it: session_context(provider, cwd, session_id))")
    return fn


# ------------------------------------------------------------ P0-R8a.1 --

def test_p0_r8a_1_shipped_defaults_carry_both_context_limits(tmp_path):
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    limits = config_mod.load(paths).limits
    assert limits.get("context_wind_down_tokens") == 150000
    assert limits.get("compact_at_tokens") == 120000


def test_p0_r8a_1_each_key_is_commented_as_tokens():
    """"with a comment each saying what they are and that they are tokens, not
    percentages": the key's own line or the comment block directly above it
    must say `token`."""
    lines = (ch.SHIPPED / "project.yaml").read_text().splitlines()
    for key in ("context_wind_down_tokens", "compact_at_tokens"):
        at = [i for i, line in enumerate(lines) if re.match(rf"\s+{key}\s*:", line)]
        assert len(at) == 1, f"{key} must appear exactly once under limits:"
        i = at[0]
        comment = [lines[i].partition("#")[2]]
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            comment.append(lines[j])
            j -= 1
        text = " ".join(comment).lower()
        assert "token" in text.replace(key, ""), (
            f"{key} has no comment saying it is a token count")


def test_p0_r8a_1_a_project_without_the_keys_inherits_them(tmp_path):
    """Silences § Existing data: no migration — a project layer that sets other
    limits still sees both keys from the defaults layer."""
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    (paths.config).mkdir(parents=True, exist_ok=True)
    (paths.config / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"max_steps": 77}}))
    limits = config_mod.load(paths).limits
    assert limits.get("max_steps") == 77
    assert limits.get("context_wind_down_tokens") == 150000
    assert limits.get("compact_at_tokens") == 120000


# ------------------------------------------------------------ P0-R8a.2 --

@pytest.fixture
def tx(tmp_path):
    """A provider declaring a transcript dir, a cwd, and that cwd's session file."""
    root = tmp_path / "transcripts"
    cwd = (tmp_path / "proj").resolve()
    cwd.mkdir()
    provider = Provider.from_dict("txp", {"bin": "txp",
                                          "transcript": ch.claude_transcript_block(root)})
    session_file = root / ch.slug(cwd) / f"{SID}.jsonl"
    return provider, cwd, session_file


def test_p0_r8a_2_returns_the_last_requests_context(tx):
    provider, cwd, path = tx
    last = ch.request(91_234)
    ch.write_transcript(path, [ch.user("hi"), ch.request(10_000), ch.tool_result(),
                               ch.request(50_000), ch.tool_result(), last,
                               # records after the last request carry no usage
                               ch.user("more"), {"type": "system", "subtype": "hook"}])
    assert _session_context()(provider, cwd, SID) == 91_234


def test_p0_r8a_2_sums_every_input_field_not_just_input_tokens(tx):
    """context_tokens(usage) is input + cache read + cache creation."""
    provider, cwd, path = tx
    rec = ch.request(0)
    rec["message"]["usage"] = {"input_tokens": 3, "cache_read_input_tokens": 70_000,
                               "cache_creation_input_tokens": 4_000,
                               "output_tokens": 999}
    ch.write_transcript(path, [ch.request(10), rec])
    assert _session_context()(provider, cwd, SID) == 74_003


def test_p0_r8a_2_after_a_compaction_the_next_request_is_the_reading(tx):
    provider, cwd, path = tx
    ch.write_transcript(path, [ch.request(140_000), ch.request(160_000),
                               ch.compaction(160_000, 2_100),
                               ch.request(4_321)])
    assert _session_context()(provider, cwd, SID) == 4_321


def test_p0_r8a_2_prefers_the_session_file_over_a_newer_sibling(tx):
    provider, cwd, path = tx
    ch.write_transcript(path, [ch.request(12_345)])
    other = path.parent / "someone-else.jsonl"
    ch.write_transcript(other, [ch.request(199_999)])
    later = time.time() + 100
    os.utime(other, (later, later))
    assert _session_context()(provider, cwd, SID) == 12_345


def test_p0_r8a_2_no_fallback_when_the_session_file_is_missing(tx):
    """Amendment: the file named by session_id and nothing else. Other
    transcripts in the directory — another role's session — are not read."""
    provider, cwd, path = tx
    ch.write_transcript(path.parent / "other-role-session.jsonl", [ch.request(55_555)])
    ch.write_transcript(path.parent / "older.jsonl", [ch.request(11_111)])
    assert not path.exists()
    assert _session_context()(provider, cwd, SID) is None


def test_p0_r8a_2_ignores_a_torn_last_line_and_junk(tx):
    """A live transcript is being appended to while it is read: a half-written
    last line is not a request, and junk lines are not either."""
    provider, cwd, path = tx
    ch.write_transcript(path, [ch.request(33_333)])
    with path.open("a") as fh:
        fh.write("not json at all\n")
        fh.write('{"type": "assistant", "message": {"usage": {"input_tokens": 99')
    assert _session_context()(provider, cwd, SID) == 33_333


def test_p0_r8a_2_no_transcript_block_is_none(tx):
    _, cwd, path = tx
    ch.write_transcript(path, [ch.request(50_000)])
    bare = Provider.from_dict("notx", {"bin": "notx"})
    assert _session_context()(bare, cwd, SID) is None


@pytest.mark.parametrize("state", ["missing", "empty", "no_usage", "other_cwd"])
def test_p0_r8a_2_no_reading_is_none_never_zero(tx, tmp_path, state):
    provider, cwd, path = tx
    if state == "empty":
        path.parent.mkdir(parents=True)
        path.write_text("")
    elif state == "no_usage":
        ch.write_transcript(path, [ch.user("hello"), ch.tool_result(),
                                   {"type": "system", "subtype": "init"}])
    elif state == "other_cwd":
        # The right session id, filed under a different directory's slug.
        elsewhere = path.parent.parent / ch.slug(tmp_path / "elsewhere") / path.name
        ch.write_transcript(elsewhere, [ch.request(70_000)])
    result = _session_context()(provider, cwd, SID)
    assert result is None, f"{state}: expected None, got {result!r}"


def test_p0_r8a_2_an_unreadable_file_is_none(tx):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    provider, cwd, path = tx
    ch.write_transcript(path, [ch.request(70_000)])
    path.chmod(0)
    try:
        assert _session_context()(provider, cwd, SID) is None
    finally:
        path.chmod(0o644)


# --------------------------------------------- the server-level fixture --

class ServerProject:
    """A real project whose MCP server runs as a launched role (depth 0)."""

    def __init__(self, tmp_path: Path, monkeypatch, *, transcript: bool = True,
                 wind_down: int = WIND_DOWN, compact_at: int = COMPACT_AT):
        self.root = h.make_git_repo((tmp_path / "proj").resolve())
        self.paths = ProjectPaths(self.root)
        self.config = self.root / ".multiagents" / "config"
        self.config.mkdir(parents=True, exist_ok=True)
        self.tx_root = tmp_path / "transcripts"
        self.transcript = self.tx_root / ch.slug(self.root) / f"{SID}.jsonl"
        provider = {"bin": "true", "spawn": {"args": ["x"]}}
        if transcript:
            provider["transcript"] = ch.claude_transcript_block(self.tx_root)
        (self.config / "providers.yaml").write_text(
            yaml.safe_dump({"providers": {"txp": provider}}))
        # The shipped launched entries are re-pointed at the test provider, so
        # whichever way the server works out the role's provider, it is txp.
        role = {"provider": "txp", "model": "m", "launch": True}
        (self.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {
            "orchestrator": {**role, "role": "orchestrator"},
            "initializer": {**role, "role": "initializer"},
        }}))
        (self.config / "project.yaml").write_text(yaml.safe_dump({
            "team": "",
            "limits": {"context_wind_down_tokens": wind_down,
                       "compact_at_tokens": compact_at}}))
        h.as_root(monkeypatch)
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        monkeypatch.setenv("MULTIAGENTS_SESSION_ID", SID)
        monkeypatch.setenv("MULTIAGENTS_ROLE", "orchestrator")
        monkeypatch.chdir(self.root)
        # The provider readers are not under test and must not reach the network.
        monkeypatch.setattr(server.budget_mod, "read_all", lambda *a, **k: {})
        self.paths.ensure()
        Tree(self.paths.tree_file, self.paths.events_file).add(Node(
            id="dr-5e5510", agent="orchestrator", provider="txp", model="m",
            parent=None, depth=0, status="running", task="orchestrator session",
            session=SID, session_id=SID, role="orchestrator",
            started_at=tree_now()))

    def notices(self) -> list[dict]:
        return ch.events(self.paths.events_file, "context_wind_down")


@pytest.fixture
def served(tmp_path, monkeypatch):
    server._reset()
    made = []

    def build(**kwargs) -> ServerProject:
        made.append(ServerProject(tmp_path, monkeypatch, **kwargs))
        return made[-1]

    yield build
    server._reset()


def _call() -> dict:
    """A cheap tool call, as the MCP host makes it."""
    return server.agent_tree()


# ------------------------------------------------------------ P0-R8a.3 --

def test_p0_r8a_3_over_the_threshold_the_next_response_carries_the_notice(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(2_000), ch.request(12_500)])
    result = _call()
    notice = result.get("context_wind_down")
    assert isinstance(notice, dict), f"no context_wind_down key in {sorted(result)}"
    assert notice.get("tokens") == 12_500
    assert notice.get("threshold") == WIND_DOWN
    instruction = notice.get("instruction")
    assert isinstance(instruction, str) and instruction.strip()
    assert "BRIEF.md" in instruction, "the handoff goes into BRIEF.md"
    emitted = p.notices()
    assert len(emitted) == 1
    assert emitted[0].get("tokens") == 12_500
    assert emitted[0].get("threshold") == WIND_DOWN


def test_p0_r8a_3_exactly_at_the_threshold_is_over(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(WIND_DOWN)])
    assert "context_wind_down" in _call()


def test_p0_r8a_3_one_under_the_threshold_is_no_notice(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(WIND_DOWN - 1)])
    assert "context_wind_down" not in _call()
    assert p.notices() == []


def test_p0_r8a_3_threshold_zero_disables_the_notice(served):
    p = served(wind_down=0)
    ch.write_transcript(p.transcript, [ch.request(900_000)])
    for _ in range(2):
        assert "context_wind_down" not in _call()
    assert p.notices() == []


def test_p0_r8a_3_the_notice_rides_any_tool_and_keeps_its_payload(served):
    """Attached "the same way config_reload is": the tool's own result is
    still all there."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(20_000)])
    result = server.list_agents()
    assert "context_wind_down" in result
    plain = {k: v for k, v in result.items() if k != "context_wind_down"}
    server._reset()
    p.transcript.unlink()
    again = server.list_agents()
    assert set(plain) - {"config_reload"} == set(again) - {"config_reload"}


# ------------------------------------------------------------ P0-R8a.4 --

def test_p0_r8a_4_once_per_crossing(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(15_000)])
    got = ["context_wind_down" in _call() for _ in range(3)]
    assert got == [True, False, False]

    # The context grows further without dropping below: still the same crossing.
    ch.append_transcript(p.transcript, [ch.request(18_000)])
    assert "context_wind_down" not in _call()

    # A compaction takes it under; a call sees that.
    ch.append_transcript(p.transcript, [ch.compaction(18_000, 1_500), ch.request(2_000)])
    assert "context_wind_down" not in _call()

    # And it crosses again.
    ch.append_transcript(p.transcript, [ch.request(11_000)])
    got = ["context_wind_down" in _call() for _ in range(3)]
    assert got == [True, False, False]
    assert len(p.notices()) == 2


def test_p0_r8a_4_a_drop_nobody_observed_is_not_a_second_crossing(served):
    """The notice re-arms when a READING is below the threshold. A compaction
    and a regrowth that both happen between two tool calls look, from the
    server, like a reading that never left the region above."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(15_000)])
    assert "context_wind_down" in _call()
    ch.append_transcript(p.transcript, [ch.compaction(15_000, 1_000),
                                        ch.request(1_500), ch.request(16_000)])
    assert "context_wind_down" not in _call()


def test_p0_r8a_4_a_restart_repeats_it_at_most_once(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(15_000)])
    assert "context_wind_down" in _call()
    server._reset()
    after_restart = ["context_wind_down" in _call() for _ in range(3)]
    assert sum(after_restart) <= 1
    assert len(p.notices()) <= 2


# ------------------------------------------------------------ P0-R8a.5 --

def _context_block() -> dict:
    status = server.budget_status()
    block = status.get("context")
    assert isinstance(block, dict), f"budget_status has no context block: {sorted(status)}"
    return block


def test_p0_r8a_5_no_transcript_declared_is_unknown_not_empty(served):
    p = served(transcript=False)
    # Even a transcript sitting exactly where one would be is not a reading
    # for a provider that declares none.
    ch.write_transcript(p.transcript, [ch.request(900_000)])
    block = _context_block()
    assert block.get("known") is False
    assert "tokens" in block and block["tokens"] is None
    assert block.get("wind_down_at") == WIND_DOWN
    assert block.get("compact_at") == COMPACT_AT
    for _ in range(2):
        assert "context_wind_down" not in _call()
    assert p.notices() == []


def test_p0_r8a_5_missing_session_file_is_unknown(served):
    served()
    block = _context_block()
    assert block.get("known") is False
    assert "tokens" in block and block["tokens"] is None


def test_p0_r8a_5_a_reading_is_reported_with_both_thresholds(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(3_000), ch.request(6_789)])
    block = _context_block()
    assert block.get("known") is True
    assert block.get("tokens") == 6_789
    assert block.get("wind_down_at") == WIND_DOWN
    assert block.get("compact_at") == COMPACT_AT


def test_p0_r8a_5_the_block_follows_the_transcript(served):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(6_000)])
    assert _context_block().get("tokens") == 6_000
    ch.append_transcript(p.transcript, [ch.compaction(6_000, 400), ch.request(700)])
    assert _context_block().get("tokens") == 700


def test_p0_r8a_5_a_non_launched_server_has_an_unknown_block(served, monkeypatch):
    """Amendment: the block is in every response; a server that is not a
    launched role (R8a.6) reports it unknown, even with a transcript at hand."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(6_000)])
    h.as_subagent(monkeypatch, agent_id="ag-sub002", depth=1)
    block = _context_block()
    assert block.get("known") is False
    assert "tokens" in block and block["tokens"] is None


def test_p0_r8a_5_no_session_id_has_an_unknown_block(served, monkeypatch):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(6_000)])
    monkeypatch.delenv("MULTIAGENTS_SESSION_ID")
    block = _context_block()
    assert block.get("known") is False
    assert "tokens" in block and block["tokens"] is None


# ------------------------------------------------------------ P0-R8a.8 --

def test_p0_r8a_8_the_reading_is_for_the_project_root_not_the_process_cwd(
        served, tmp_path, monkeypatch):
    """Amendment P0-R8a.8: the directory is the project root. The server's
    process cwd is elsewhere, and a decoy session with the same id sits under
    that directory's slug; the reading is the root's."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(12_345)])
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    ch.write_transcript(p.tx_root / ch.slug(elsewhere) / f"{SID}.jsonl",
                        [ch.request(2_000)])
    monkeypatch.chdir(elsewhere)
    notice = _call().get("context_wind_down")
    assert isinstance(notice, dict) and notice.get("tokens") == 12_345
    assert _context_block().get("tokens") == 12_345


def test_p0_r8a_8_the_provider_is_the_launched_roles_roster_entry(served, tmp_path):
    """Amendment P0-R8a.8: the provider is the one `agents.yaml` gives the role
    named by MULTIAGENTS_ROLE — not the driver node's `provider` field. Here
    the node names a decoy provider whose transcripts say something else."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(12_345)])
    decoy_root = tmp_path / "decoy-transcripts"
    ch.write_transcript(decoy_root / ch.slug(p.root) / f"{SID}.jsonl", [ch.request(1_000)])
    providers = yaml.safe_load((p.config / "providers.yaml").read_text())
    providers["providers"]["decoy"] = {"bin": "true", "spawn": {"args": ["x"]},
                                       "transcript": ch.claude_transcript_block(decoy_root)}
    (p.config / "providers.yaml").write_text(yaml.safe_dump(providers))
    tree = Tree(p.paths.tree_file, p.paths.events_file)
    with tree.transaction() as data:
        data["nodes"]["dr-5e5510"]["provider"] = "decoy"
    assert _context_block().get("tokens") == 12_345
    notice = _call().get("context_wind_down")
    assert isinstance(notice, dict) and notice.get("tokens") == 12_345


# ------------------------------------------------------------ P0-R8a.6 --

def test_p0_r8a_6_a_subagent_server_never_reads_or_notifies(served, monkeypatch):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(90_000)])
    h.as_subagent(monkeypatch, agent_id="ag-sub001", depth=1)
    counter = ch.ReadCounter(p.transcript).install(monkeypatch)
    for _ in range(3):
        assert "context_wind_down" not in _call()
    assert counter.bytes == 0 and counter.opens == 0, "a subagent read the transcript"
    assert p.notices() == []


def test_p0_r8a_6_no_session_id_never_reads_or_notifies(served, monkeypatch):
    p = served()
    ch.write_transcript(p.transcript, [ch.request(90_000)])
    monkeypatch.delenv("MULTIAGENTS_SESSION_ID")
    counter = ch.ReadCounter(p.transcript).install(monkeypatch)
    for _ in range(3):
        assert "context_wind_down" not in _call()
    assert counter.bytes == 0 and counter.opens == 0
    assert p.notices() == []


def test_p0_r8a_6_an_explicit_depth_zero_is_the_launched_role(served, monkeypatch):
    p = served()
    monkeypatch.setenv("MULTIAGENTS_DEPTH", "0")
    ch.write_transcript(p.transcript, [ch.request(90_000)])
    assert "context_wind_down" in _call()


# ------------------------------------------------------------ P0-R8a.7 --

def _big_transcript(path: Path, megabytes: int = 50) -> int:
    """A transcript of about `megabytes` MB whose last request is 12,000."""
    pad = "x" * 900
    chunk = "".join(
        __import__("json").dumps(ch.request(5_000 + i, text=pad)) + "\n"
        for i in range(200))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        written = 0
        while written < megabytes * 1024 * 1024:
            fh.write(chunk)
            written += len(chunk)
        fh.write(__import__("json").dumps(ch.request(12_000)) + "\n")
    return path.stat().st_size


def _timed(fn, n: int = 5) -> float:
    times = []
    for _ in range(n):
        began = time.perf_counter()
        fn()
        times.append(time.perf_counter() - began)
    return statistics.median(times)


def test_p0_r8a_7_an_unchanged_transcript_is_not_read_again(served, monkeypatch):
    p = served()
    size = _big_transcript(p.transcript)
    assert size >= 50 * 1024 * 1024

    first = _call()                               # may read it all, once
    assert "context_wind_down" in first, "the reading of the big file is 12,000"

    counter = ch.ReadCounter(p.transcript).install(monkeypatch)
    unchanged = _timed(_call)
    assert counter.bytes == 0, (
        f"{counter.bytes} bytes of an unchanged transcript were read again")

    # Baseline: the same call on the same server with the reading switched off
    # (no session id means no read at all, per P0-R8a.6).
    monkeypatch.delenv("MULTIAGENTS_SESSION_ID")
    baseline = _timed(_call)
    assert unchanged - baseline < 0.100, (
        f"an unchanged 50 MB transcript added {1000 * (unchanged - baseline):.0f} ms")


def test_p0_r8a_7_a_grown_transcript_reads_only_the_new_tail(served, monkeypatch):
    p = served()
    _big_transcript(p.transcript)
    _call()
    assert _context_block().get("tokens") == 12_000

    tail = [ch.compaction(12_000, 900), ch.request(1_234)]
    ch.append_transcript(p.transcript, tail)
    counter = ch.ReadCounter(p.transcript).install(monkeypatch)
    assert _context_block().get("tokens") == 1_234
    assert counter.bytes < 1024 * 1024, (
        f"{counter.bytes} bytes read to take in a tail of well under 1 KB")


def test_p0_r8a_7_a_rewritten_file_is_not_mistaken_for_growth(served):
    """Reuse is keyed on (mtime, size). A file replaced by a SHORTER one — a
    new session under the same id, or a truncation — must be read afresh, not
    have its 'tail' read from a stale offset."""
    p = served()
    ch.write_transcript(p.transcript, [ch.request(3_000)] * 20 + [ch.request(9_000)])
    assert _context_block().get("tokens") == 9_000
    ch.write_transcript(p.transcript, [ch.request(4_444)])
    later = time.time() + 50
    os.utime(p.transcript, (later, later))
    assert _context_block().get("tokens") == 4_444


# ------------------------------------------------------------ P0-R8b.1 --

def _orchestrator_prompt(tmp_path: Path, team: str) -> str:
    from multiagents import driver

    paths = ProjectPaths(tmp_path)
    paths.ensure()
    config = config_mod.load(paths)
    spec = driver._launched_spec(config, "orchestrator", team)
    assert spec is not None
    return config.instructions_for(spec)


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == heading]
    assert starts, f"no {heading!r} heading in the composed orchestrator prompt"
    body = []
    for line in lines[starts[0] + 1:]:
        if re.match(r"#{1,2} ", line):
            break
        body.append(line)
    return "\n".join(body)


def test_p0_r8b_1_the_implement_orchestrator_is_told_what_survives(tmp_path):
    section = _section(_orchestrator_prompt(tmp_path, "implement"),
                       "## Your own context window")
    for name in ("BRIEF.md", "list_findings", "list_tickets", "agent_tree",
                 "context_wind_down", "/compact"):
        assert name in section, f"{name} is not named under the heading"
    lowered = section.lower()
    assert "record it when you decide it" in lowered
    assert "unattended" in lowered


def test_p0_r8b_1_the_section_is_in_the_shared_brief(tmp_path):
    """It lives in `_orchestrator.md`, so every team's orchestrator has it."""
    text = (ch.SHIPPED / "agents" / "team" / "_orchestrator.md").read_text()
    assert any(line.strip() == "## Your own context window" for line in text.splitlines())
    assert "## Your own context window" in _orchestrator_prompt(tmp_path, "review")


# ------------------------------------------------------------ invariant --

BASE = "f5b58cf"
WORDS = ("claude", "agy", "opencode", "compact_boundary", "compactMetadata", "/compact")


def _count(text: str, word: str) -> int:
    return len(re.findall(re.escape(word), text, flags=re.IGNORECASE))


def test_p0_r8_invariant_no_provider_or_transcript_vocabulary_added():
    """Contract header: nothing in src/multiagents/*.py or executor/*.py gains
    a provider name or a transcript word. Measured against the commit this
    contract was written on: an existing mention may stay, none may be added."""
    import subprocess

    src = ch.REPO / "src" / "multiagents"
    probe = subprocess.run(["git", "cat-file", "-e", f"{BASE}^{{commit}}"],
                           cwd=ch.REPO, capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"base commit {BASE} is not in this clone")
    added = []
    for path in sorted([*src.glob("*.py"), *(src / "executor").glob("*.py")]):
        rel = path.relative_to(ch.REPO).as_posix()
        old = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ch.REPO,
                             capture_output=True, text=True)
        before = old.stdout if old.returncode == 0 else ""
        now_text = path.read_text()
        for word in WORDS:
            if _count(now_text, word) > _count(before, word):
                added.append(f"{rel}: {word!r} {_count(before, word)} -> "
                             f"{_count(now_text, word)}")
    assert not added, "provider/transcript vocabulary added:\n" + "\n".join(added)
