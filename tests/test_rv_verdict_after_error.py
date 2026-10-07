"""RV: a complete verdict survives a transport error at the end of the stream.

Contract: context/specs/verdict-after-transport-error.md (RV-R1..RV-R5).

Streams have the shape recorded in the agy-b reviewer runs ag-8bc769 /
ag-0a7301 / ag-0a5e9b: the CLI exits 0 and its only `result` event carries
`status: "ERROR"`, an `error` string, and a `response` that holds the whole
review. A fake CLI replays those events through the real runner; everything
asserted is a node status, a persisted result, a tool payload or the provider
breaker's counters.
"""

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h

RUN_TIMEOUT = 15

ERROR_503 = "API error (attempt 1): UNAVAILABLE (code 503): The service is currently unavailable."
ERROR_EOF = 'API error (attempt 1): request failed: Post "https://example.invalid/v1": EOF'
ERROR_INTERRUPTED = "The stream was interrupted. Please continue the task you were working on."

REVIEW = "## Findings\n\n1. BLOCKING: the retry path drops the lock.\n2. BLOCKING: no test.\n\n"
REJECTED = REVIEW + "VERDICT(rejected, 2): two blocking defects.\n"
APPROVED = "## Findings\n\nNothing blocking.\n\nVERDICT(approved, 0): no findings.\n"

# agy's stream rules, as shipped: the final `result` event is nested.
AGY_RULES = [
    {"match": {"event": "result"}, "as": "result",
     "fields": {"status": "result.status", "text": "result.response",
                "tokens": "result.usage"}},
    {"match": {"event": "init"}, "as": "step", "fields": {}},
]
# A second provider with a different, flat shape (RV-R5).
FLAT_RULES = [
    {"match": {"type": "result"}, "as": "result",
     "fields": {"status": "subtype", "text": "result"}},
    {"match": {"type": "text"}, "as": "text", "fields": {"text": "text"}},
]


def agy_events(status, response, error=""):
    return [
        {"event": "init", "init": {"model": "m", "cwd": "/w"},
         "conversation_id": "c-1"},
        {"event": "result", "result": {
            "conversation_id": "c-1", "status": status, "response": response,
            "error": error, "duration_seconds": 12.5, "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
    ]


def flat_events(status, response, error=""):
    return [{"type": "text", "text": "Review started."},
            {"type": "result", "subtype": status, "result": response, "error": error}]


def scenario(status="ERROR", response=REJECTED, error=ERROR_503, exit_code=0,
             stderr="", shape="agy"):
    build = agy_events if shape == "agy" else flat_events
    return {"events": build(status, response, error), "exit": exit_code, "stderr": stderr}


def make_cli(tmp_path, scenarios, shape="agy"):
    """A CLI that plays scenario n on its n-th invocation (state in a file)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "rvcli.py"
    counter = tmp_path / "rvcli.count"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, pathlib\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"s = json.loads({json.dumps(scenarios)!r})[min(n, {len(scenarios) - 1})]\n"
        "for e in s['events']:\n"
        "    print(json.dumps(e)); sys.stdout.flush()\n"
        "sys.stderr.write(s['stderr'])\n"
        "sys.exit(s['exit'])\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"bin": str(script), "spawn": {"args": ["--fake-cli"]},
            "stream": {"format": "ndjson",
                       "session_id_paths": ["conversation_id", "result.conversation_id"],
                       "rules": AGY_RULES if shape == "agy" else FLAT_RULES}}


def run_all(tmp_path, monkeypatch, scenarios, shape="agy", provider="fake"):
    """Run one agent per scenario, in order. Returns (runner, [agent ids])."""
    runner = h.make_runner(
        tmp_path / "project", monkeypatch,
        agents={"reviewer": h.AgentSpec("reviewer", provider, "m")},
        providers={provider: make_cli(tmp_path, scenarios, shape)},
    )

    async def go():
        ids = []
        for _ in scenarios:
            started = await runner.start("reviewer", "review it")
            agent = started["agent_id"]
            await asyncio.wait_for(runner.runs[agent].task, timeout=RUN_TIMEOUT)
            ids.append(agent)
        return ids

    return runner, asyncio.run(go())


def run_one(tmp_path, monkeypatch, **kw):
    shape = kw.pop("shape", "agy")
    runner, ids = run_all(tmp_path, monkeypatch, [scenario(shape=shape, **kw)], shape=shape)
    return runner, ids[0]


def failures(runner, provider="fake"):
    return runner.tree.provider_health().get(provider, {}).get("consecutive_failures", 0)


def tripped(runner, provider="fake"):
    return bool(runner.tree.provider_health().get(provider, {}).get("tripped"))


def persisted(runner, agent):
    return json.loads((runner.paths.run_dir(agent) / "result.json").read_text())


# ---------------------------------------------------------------- RV-R1

@pytest.mark.parametrize("response, verdict, defects", [
    (REJECTED, "rejected", 2),
    (APPROVED, "approved", 0),
])
@pytest.mark.parametrize("error", [ERROR_503, ERROR_EOF, ERROR_INTERRUPTED])
def test_rv_r1_complete_verdict_after_transport_error_is_done(
        tmp_path, monkeypatch, response, verdict, defects, error):
    runner, agent = run_one(tmp_path, monkeypatch, response=response, error=error)
    node = runner.tree.get(agent)
    assert node.status == "done"
    assert persisted(runner, agent)["status"] == "done"
    assert node.verdict == verdict
    assert node.defects == defects
    assert runner.collect(agent)["status"] == "done"
    assert runner.check(agent)["status"] == "done"


def test_rv_r1_verdict_matches_a_clean_run_of_the_same_review(tmp_path, monkeypatch):
    clean_runner, clean = run_one(tmp_path / "clean", monkeypatch, status="SUCCESS",
                                  error="")
    err_runner, errored = run_one(tmp_path / "err", monkeypatch)
    a, b = clean_runner.tree.get(clean), err_runner.tree.get(errored)
    assert (a.status, a.verdict, a.defects) == (b.status, b.verdict, b.defects)
    assert b.status == "done"
    assert clean_runner.collect(clean, mode="full")["text"] == \
        err_runner.collect(errored, mode="full")["text"]


@pytest.mark.parametrize("status", ["ERROR", "FAILED", "INTERRUPTED"])
def test_rv_r1_any_non_success_final_status_with_a_verdict(tmp_path, monkeypatch, status):
    runner, agent = run_one(tmp_path, monkeypatch, status=status)
    assert runner.tree.get(agent).status == "done"
    assert runner.tree.get(agent).verdict == "rejected"


def test_rv_r1_last_verdict_wins_when_the_review_quotes_an_earlier_one(
        tmp_path, monkeypatch):
    text = "Format is VERDICT(approved, 0): x\n\n" + "VERDICT(rejected, 3): three.\n"
    runner, agent = run_one(tmp_path, monkeypatch, response=text)
    node = runner.tree.get(agent)
    assert (node.status, node.verdict, node.defects) == ("done", "rejected", 3)


# ---------------------------------------------------------------- RV-R2

def _strings(value):
    """Every decoded string value (keys included) nested in a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)



def test_rv_r2_provider_error_text_is_recorded_in_the_result(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, error=ERROR_503)
    assert runner.tree.get(agent).status == "done"
    assert ERROR_503 in json.dumps(persisted(runner, agent))


def test_rv_r2_collect_shows_the_error_in_both_modes(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, error=ERROR_EOF)
    assert runner.tree.get(agent).status == "done"
    # compare against decoded strings: json.dumps escapes the quotes in ERROR_EOF
    assert any(ERROR_EOF in v for v in _strings(runner.collect(agent)))
    assert any(ERROR_EOF in v for v in _strings(
        {k: v for k, v in runner.collect(agent, mode="full").items() if k != "text"}))


def test_rv_r2_check_shows_the_error_outside_the_event_window(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, error=ERROR_INTERRUPTED)
    assert runner.tree.get(agent).status == "done"
    payload = {k: v for k, v in runner.check(agent).items() if k != "events"}
    assert ERROR_INTERRUPTED in json.dumps(payload)


def test_rv_r2_the_verdict_text_itself_is_not_altered(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch)
    assert runner.tree.get(agent).status == "done"
    assert runner.collect(agent, mode="full")["text"].rstrip().endswith(
        "VERDICT(rejected, 2): two blocking defects.")


def test_rv_r2_a_clean_run_records_no_error(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, status="SUCCESS", error="")
    assert runner.tree.get(agent).status == "done"
    assert not persisted(runner, agent).get("warnings")
    assert "warnings" not in runner.collect(agent)


# ---------------------------------------------------------------- RV-R3

def test_rv_r3_three_in_a_row_leave_the_breaker_closed(tmp_path, monkeypatch):
    runner, ids = run_all(tmp_path, monkeypatch, [scenario()] * 3)
    assert [runner.tree.get(a).status for a in ids] == ["done"] * 3
    assert failures(runner) == 0
    assert not tripped(runner)
    assert "fake" not in runner.tree.read().get("cooldowns", {})


def test_rv_r3_five_in_a_row_with_alternating_verdicts(tmp_path, monkeypatch):
    runner, ids = run_all(tmp_path, monkeypatch,
                          [scenario(response=r) for r in [REJECTED, APPROVED] * 2 + [REJECTED]])
    assert all(runner.tree.get(a).status == "done" for a in ids)
    assert failures(runner) == 0 and not tripped(runner)


def test_rv_r3_resets_failures_the_way_a_success_does(tmp_path, monkeypatch):
    plain = scenario(response=REVIEW)               # no verdict: a failure
    runner, ids = run_all(tmp_path, monkeypatch, [plain, plain, scenario()])
    assert [runner.tree.get(a).status for a in ids] == ["failed", "failed", "done"]
    assert failures(runner) == 0


def test_rv_r3_failures_before_and_after_do_not_hide_the_success(tmp_path, monkeypatch):
    plain = scenario(response=REVIEW)
    runner, ids = run_all(tmp_path, monkeypatch, [plain, plain, scenario(), plain, plain])
    assert failures(runner) == 2
    assert not tripped(runner)


# ---------------------------------------------------------------- RV-R4

@pytest.mark.parametrize("response", [
    pytest.param(REVIEW, id="no-verdict"),
    pytest.param(REVIEW + "VERDICT: Findings present. New defects in crash recovery.\n",
                 id="verdict-without-parenthesis-recorded-ag-0a5e9b"),
    pytest.param(REVIEW + "VERDICT(maybe, 2): unsure.\n", id="unknown-verdict-word"),
    pytest.param(REVIEW + "VERDICT(rejected, 2", id="cut-off-mid-verdict"),
    pytest.param(REVIEW + "VERDICT(", id="cut-off-at-the-opening"),
    pytest.param(ERROR_503, id="final-text-is-only-the-error"),
])
def test_rv_r4_without_a_complete_verdict_the_run_still_fails(
        tmp_path, monkeypatch, response):
    runner, agent = run_one(tmp_path, monkeypatch, response=response)
    assert runner.tree.get(agent).status == "failed"
    assert persisted(runner, agent)["status"] == "failed"
    assert failures(runner) == 1


def test_rv_r4_an_empty_response_is_not_a_verdict(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, response="")
    assert runner.tree.get(agent).status != "done"


def test_rv_r4_three_unverdicted_errors_still_trip_the_breaker(tmp_path, monkeypatch):
    runner, ids = run_all(tmp_path, monkeypatch, [scenario(response=REVIEW)] * 3)
    assert [runner.tree.get(a).status for a in ids] == ["failed"] * 3
    assert failures(runner) == 3
    assert tripped(runner)


def test_rv_r4_non_zero_exit_with_a_verdict_still_fails(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, exit_code=1)
    assert runner.tree.get(agent).status == "failed"
    assert failures(runner) == 1


def test_rv_r4_non_zero_exit_with_a_verdict_and_success_status_is_unchanged(
        tmp_path, monkeypatch):
    # Control: today's behaviour for a crash after a clean result is untouched,
    # whatever it is — compare against a run with no verdict at all.
    with_verdict, a = run_one(tmp_path / "a", monkeypatch, status="SUCCESS", error="",
                              exit_code=1)
    without, b = run_one(tmp_path / "b", monkeypatch, status="SUCCESS", error="",
                         exit_code=1, response=REVIEW)
    assert with_verdict.tree.get(a).status == without.tree.get(b).status


@pytest.mark.parametrize("status, expected", [("REFUSED", "refused"),
                                              ("TRUNCATED", "truncated")])
def test_rv_r4_refused_and_truncated_keep_their_status_and_stay_off_the_breaker(
        tmp_path, monkeypatch, status, expected):
    runner, agent = run_one(tmp_path, monkeypatch, status=status)
    assert runner.tree.get(agent).status == expected
    assert persisted(runner, agent)["status"] == expected
    assert failures(runner) == 0


def test_rv_r4_truncation_marker_on_stderr_beats_the_verdict_rule(tmp_path, monkeypatch):
    # Truncation detected from stderr (a clean-looking exit 0) keeps winning.
    marker = "response truncated"
    cli_runner = h.make_runner(
        tmp_path / "project", monkeypatch,
        agents={"reviewer": h.AgentSpec("reviewer", "fake", "m")},
        providers={"fake": {**make_cli(tmp_path, [scenario(stderr=marker)]),
                            "truncation_markers": [marker]}},
    )

    async def go():
        started = await cli_runner.start("reviewer", "go")
        await asyncio.wait_for(cli_runner.runs[started["agent_id"]].task, RUN_TIMEOUT)
        return started["agent_id"]

    agent = asyncio.run(go())
    assert cli_runner.tree.get(agent).status == "truncated"


def test_rv_r4_quota_exhaustion_is_not_turned_into_done(tmp_path, monkeypatch):
    runner, agent = run_one(
        tmp_path, monkeypatch, error="You've hit your monthly spend limit",
        stderr="You've hit your monthly spend limit · resets 5:50pm")
    assert runner.tree.get(agent).status != "done"


# ---------------------------------------------------------------- RV-R5

@pytest.mark.parametrize("response, verdict, defects", [
    (REJECTED, "rejected", 2), (APPROVED, "approved", 0)])
def test_rv_r5_another_provider_and_stream_shape(
        tmp_path, monkeypatch, response, verdict, defects):
    runner, agent = run_one(tmp_path, monkeypatch, shape="flat", response=response)
    node = runner.tree.get(agent)
    assert (node.status, node.verdict, node.defects) == ("done", verdict, defects)
    assert ERROR_503 in json.dumps(persisted(runner, agent))


def test_rv_r5_breaker_of_that_provider_stays_closed(tmp_path, monkeypatch):
    runner, ids = run_all(tmp_path, monkeypatch, [scenario(shape="flat")] * 3,
                          shape="flat", provider="other")
    assert all(runner.tree.get(a).status == "done" for a in ids)
    assert failures(runner, "other") == 0 and not tripped(runner, "other")


def test_rv_r5_other_provider_without_verdict_still_fails(tmp_path, monkeypatch):
    runner, agent = run_one(tmp_path, monkeypatch, shape="flat", response=REVIEW)
    assert runner.tree.get(agent).status == "failed"
    assert failures(runner) == 1
