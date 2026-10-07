"""TB-R4 (tooling batch 2026-10, package B): a provider rate limit defers a
node run; it does not fail it.

Contract: `context/specs/tooling-batch-2026-10.md`, TB-R4 and its "Verified by"
line. Runs are fixture-agent processes (`tests/nc_fixture`, M4 queue flavour)
with one extra directive of this file's own, scripted per activation through
the provider queue:

    {"rl": {"retry_after": <seconds>|None, "style": "429"|"cli"}}
        the run does some work (one step with 111 input / 222 output tokens),
        then dies on stderr with a rate-limit signal and exit 1:
        "429": `Error: HTTP 429 Too Many Requests` (+ `Retry-After: N`)
        "cli":  `Error: rate limited - the request will be retried`
    {"resume_fails": true}
        a resumed activation (`-s <id>`) dies at once: `session not found`.

A resumed activation does not repeat the first prompt, so the fixture plays
the provider's session memory: a resumed run sees the prompt of the session it
resumes in front of its own (verdict prompts then carry their generation).

Assumptions where the contract is silent (kept loose; each is named in the
test that uses it):
- WHERE `cause = rate_limited` is recorded is not specified; it is looked for
  in the node's transitions/events and in the run's `tree.json` entry.
- the node's state while the resume is pending is not specified; only that it
  is not final, has no outcome and needs no operator.
- "the old run directory" is looked for as the old run's id or its worktree
  path in the fresh relaunch's prompt.
- the default cooldown has no named knob. NEED_INFO(knob): these tests set
  `limits.rate_limit_cooldown_seconds` (COOLDOWN_KEY) to seconds, so that no
  test sleeps through a shipped default. If the developer names it otherwise,
  this one constant moves.
- tests that need "not before Retry-After" measure the gap between the first
  activation's start and the resumed activation's start, both from the calls
  log; the 429 is raised after the start, so a correct gap is at least the
  Retry-After.

Not expressed: the default cooldown's value, the backoff factor and its cap
(only: the second wait is longer than the knob, and finite), an HTTP-date or
malformed Retry-After, how many repeats are tolerated before giving up.
"""
from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import alive  # noqa: E402
from nc_fixture.m4_agent import M4Provider  # noqa: E402
from nc_fixture.m4_world import (M4World, commit_entry, verdict_entry)  # noqa: E402

COOLDOWN_KEY = "rate_limit_cooldown_seconds"

# Every wait is bounded explicitly so a red test fails in seconds. WAIT is one
# scheduler reaction (tick 1 s); RESUME_WAIT covers a Retry-After of up to 5 s
# plus the tick that notices it; LONG_WAIT covers a chain (backoff, fallback).
WAIT = 8
RESUME_WAIT = 12
LONG_WAIT = 20

_RL_BLOCK = r'''
try:
    if resume and os.path.exists(base + "/prompt." + resume):
        prompt = open(base + "/prompt." + resume).read() + "\n" + prompt
    elif not resume:
        open(base + "/prompt." + session, "w").write(prompt)
except OSError:
    pass
if fx.get("resume_fails") and resume:
    sys.stderr.write("Error: session not found: %s\n" % resume)
    sys.exit(1)
_rl = fx.get("rl")
if _rl:
    emit("step_start", {"id": "prt_s%d" % os.getpid(), "type": "step-start"})
    emit("step_finish", {"id": "prt_f%d" % os.getpid(), "type": "step-finish",
                         "reason": "tool-calls", "cost": 0,
                         "tokens": {"input": 111, "output": 222, "reasoning": 0,
                                    "cache": {"read": 0, "write": 0}}})
    emit("text", {"id": "prt_w%d" % os.getpid(), "type": "text", "text": "working"})
    if _rl.get("style") == "cli":
        sys.stderr.write("Error: rate limited - the request will be retried\n")
    else:
        sys.stderr.write("Error: HTTP 429 Too Many Requests\n")
        if _rl.get("retry_after") is not None:
            sys.stderr.write("Retry-After: %s\n" % _rl["retry_after"])
    sys.stderr.flush()
    sys.exit(1)
'''


class RLProvider(M4Provider):
    """The M4 fixture provider plus the `rl` / `resume_fails` directives."""

    def __init__(self, tmp: Path, name: str, sock: Path, **extra):
        super().__init__(tmp, name, sock, **extra)
        script = self.dir / "agent.py"
        text = script.read_text()
        anchor = 'emit("step_start"'
        assert anchor in text
        script.write_text(text.replace(anchor, _RL_BLOCK + anchor, 1))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


class RLWorld(M4World):
    def provider(self, name, **extra):
        fx = RLProvider(self.tmp, name, self.sock, **extra)
        self.providers[name] = fx
        return fx


def rl(retry_after=None, style: str = "429") -> dict:
    return {"rl": {"retry_after": retry_after, "style": style}}


def make_world(tmp_path, monkeypatch, *, cooldown: float = 2, threshold: int | None = None,
               max_concurrent: int | None = None) -> RLWorld:
    world = RLWorld(tmp_path, monkeypatch)
    world.project["limits"][COOLDOWN_KEY] = cooldown
    if threshold is not None:
        world.project["limits"]["provider_failure_threshold"] = threshold
    extra = {"max_concurrent": max_concurrent} if max_concurrent else {}
    world.rl = world.provider("rl", **extra)
    world.agent("rlagent", "rl", writes=True)
    # a loop's worker and reviewer, each on a provider of its own
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    return world


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


# ------------------------------------------------------------------ helpers

def first_call(w, fx=None, timeout=WAIT) -> dict:
    fx = fx or w.rl
    return w.until(lambda: (fx.calls() or [None])[0], timeout, what="the first activation")


def interrupted(w, fx=None, timeout=WAIT) -> dict:
    """The first activation's call, once its process is gone (it hit the 429)."""
    call = first_call(w, fx, timeout)
    w.until(lambda: not alive(call["pid"]), timeout, what="the 429 run to exit")
    return call


def nth_call(w, n: int, fx=None, timeout=RESUME_WAIT, node: str | None = None) -> dict:
    """Activation #n's call. With `node`, a node that turns final first fails
    the wait at once (a red run must not sit out the timeout)."""
    fx = fx or w.rl

    def settled():
        if node and w.get(node)["state"] in ("done", "cancelled"):
            return f"{node} is final ({w.get(node).get('outcome')!r}) and activation #{n} never came"
        return None
    return w.until(lambda: len(fx.calls()) >= n and fx.calls()[n - 1], timeout,
                   what=f"activation #{n}", give_up=settled)


def is_pending(w, node: str) -> dict:
    """The node is alive and unsettled: not final, no outcome, not failed."""
    n = w.get(node)
    assert n["state"] not in ("done", "cancelled"), (n["state"], n.get("outcome"), w.transitions(node))
    assert n.get("outcome") is None, n
    assert "failed" not in w.transitions(node)
    return n


def cause_blob(w, node: str) -> str:
    """Everything durable that mentions the node or its runs: transitions,
    events, the runs' `tree.json` entries."""
    runs = [r["run_id"] for r in w.get(node).get("runs", [])]
    ents = [e for e in w.events(prefix="")
            if node in json.dumps(e) or any(r in json.dumps(e) for r in runs)]
    tree = [v for k, v in w.tree_nodes().items() if k in runs or v.get("node_id") == node]
    return json.dumps([ents, tree], default=str)


def health(w, provider: str = "rl") -> dict:
    return w.tree_json().get("provider_health", {}).get(provider, {})


# ============================================ recording: cause, not failure

@pytest.mark.parametrize("style", ["429", "cli"])
def test_tb_r4_a_rate_limited_run_is_recorded_with_cause_rate_limited_not_failed(w, style):
    w.rl.queue(rl(retry_after=60 if style == "429" else None, style=style))
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    interrupted(w)
    w.until(lambda: "rate_limited" in cause_blob(w, node), WAIT,
            what="cause `rate_limited` recorded against the run")
    w.quiet(1.2)
    n = is_pending(w, node)
    assert (n.get("hold") or {}).get("reason") not in ("failed", "run_failed")
    runs = [v for k, v in w.tree_nodes().items() if v.get("node_id") == node]
    assert runs and all(r.get("status") != "failed" for r in runs), runs
    assert w.rl.spawns() == 1, "a rate limit must not be retried before its wait"


def test_tb_r4_a_rate_limit_in_a_clean_answer_is_not_a_rate_limit(w):
    """Precision guard: an agent that merely writes about 429s is done."""
    w.rl.queue({"text": "docs: HTTP 429 Too Many Requests means rate limited; the request will be retried"})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    done = w.wait_state(node, "done", WAIT)
    assert done["outcome"] != "failed"
    assert w.rl.spawns() == 1
    assert "rate_limited" not in cause_blob(w, node)


def test_tb_r4_a_ordinary_failure_is_still_a_failure_and_counted(w):
    """Precision guard: only a rate-limit signal defers; a crash still fails."""
    w.rl.queue({"crash": True})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    done = w.wait_state(node, "done", WAIT)
    assert done["outcome"] == "failed"
    assert w.rl.spawns() == 1
    assert health(w).get("consecutive_failures") == 1


# ===================================================== resume: when and how

def test_tb_r4_with_retry_after_the_same_session_resumes_after_it_and_not_before(w):
    w.rl.queue(rl(retry_after=3), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    # the cooldown knob (2 s) is shorter than Retry-After (3 s): not before 3
    time.sleep(1.0)
    is_pending(w, node)
    assert w.rl.spawns() == 1, "resumed before the Retry-After had passed"
    c2 = nth_call(w, 2, node=node)
    assert c2["t"] - c1["t"] >= 3.0, c2["t"] - c1["t"]
    assert c2["resume"] == c1["session"], "not a resume of the same session"
    done = w.wait_state(node, "done", WAIT)
    assert done["outcome"] != "failed"
    assert w.rl.spawns() == 2


def test_tb_r4_retry_after_zero_overrides_a_longer_default_cooldown(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, cooldown=30)
    try:
        world.rl.queue(rl(retry_after=0), {})
        world.start_scheduler()
        node = world.simple("A", "rlagent")
        c1 = interrupted(world)
        c2 = nth_call(world, 2, timeout=WAIT, node=node)
        assert c2["resume"] == c1["session"]
        assert world.wait_state(node, "done", WAIT)["outcome"] != "failed"
    finally:
        world.close()


def test_tb_r4_without_retry_after_the_default_cooldown_applies(w):
    w.rl.queue(rl(retry_after=None), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    time.sleep(1.0)
    is_pending(w, node)
    assert w.rl.spawns() == 1, "resumed inside the cooldown (knob: 2 s)"
    c2 = nth_call(w, 2, node=node)
    assert c2["t"] - c1["t"] >= 1.9, c2["t"] - c1["t"]
    assert c2["resume"] == c1["session"]
    assert w.wait_state(node, "done", WAIT)["outcome"] != "failed"


def test_tb_r4_the_cli_message_without_retry_after_also_waits_and_resumes(w):
    w.rl.queue(rl(style="cli"), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    c2 = nth_call(w, 2, node=node)
    assert c2["t"] - c1["t"] >= 1.9
    assert c2["resume"] == c1["session"]
    assert w.wait_state(node, "done", WAIT)["outcome"] != "failed"


def test_tb_r4_a_repeated_429_backs_off_and_still_never_fails(w):
    w.rl.queue(rl(), rl(), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    c2 = nth_call(w, 2, node=node)
    w.until(lambda: not alive(c2["pid"]), WAIT, what="the second 429 run to exit")
    is_pending(w, node)
    c3 = nth_call(w, 3, timeout=LONG_WAIT, node=node)
    g1, g2 = c2["t"] - c1["t"], c3["t"] - c2["t"]
    assert g1 >= 1.9, g1
    assert g2 >= 2.5, f"no backoff on a repeat: first wait {g1:.1f}s, second {g2:.1f}s (knob 2 s)"
    assert g2 < 30, "the backoff is not bounded"
    assert c2["resume"] == c1["session"] and c3["resume"] == c1["session"]
    done = w.wait_state(node, "done", WAIT)
    assert done["outcome"] != "failed"
    assert w.rl.spawns() == 3


# ================================================ counted / not counted / usage

def test_tb_r4_rate_limits_are_not_counted_against_the_failure_breaker(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, cooldown=1, threshold=1)
    try:
        world.rl.queue(rl(), rl(), {})
        world.start_scheduler()
        node = world.simple("A", "rlagent")
        c1 = interrupted(world)
        world.quiet(0.6)
        assert health(world).get("consecutive_failures", 0) == 0, health(world)
        c2 = nth_call(world, 2, node=node)
        world.until(lambda: not alive(c2["pid"]), WAIT, what="the second 429 run to exit")
        world.quiet(0.6)
        assert health(world).get("consecutive_failures", 0) == 0, health(world)
        assert not health(world).get("tripped")
        # at threshold 1 a misfiled failure would have opened the breaker and
        # cooled the provider down for 30 minutes: the resume could not happen
        nth_call(world, 3, timeout=LONG_WAIT, node=node)
        assert world.wait_state(node, "done", WAIT)["outcome"] != "failed"
        assert "provider_down" not in json.dumps(world.tree_json().get("cooldowns", {}))
        assert health(world).get("consecutive_failures", 0) == 0
        assert c1["resume"] is None
    finally:
        world.close()


def test_tb_r4_usage_of_the_interrupted_run_is_still_counted(w):
    w.rl.queue(rl(retry_after=1), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    assert w.wait_state(node, "done", LONG_WAIT)["outcome"] != "failed"
    usage = json.dumps([v.get("usage") for v in w.tree_nodes().values()
                        if v.get("node_id") == node], default=str)
    assert "111" in usage and "222" in usage, usage
    assert c1["resume"] is None


# ======================================================= released meanwhile

def test_tb_r4_locks_are_released_while_the_resume_is_pending(w):
    w.rl.queue(rl(retry_after=60), {"gate": "b"})
    w.start_scheduler()
    a = w.simple("A", "rlagent", locks=["L"])
    interrupted(w)
    b = w.simple("B", "rlagent", locks=["L"], fx={"gate": "b"})
    w.wait_running(b, WAIT)
    is_pending(w, a)
    w.rl.open_gate("b")
    w.wait_state(b, "done", WAIT)


def test_tb_r4_the_provider_slot_is_released_while_the_resume_is_pending(tmp_path, monkeypatch):
    """Silence: this also needs the rate limit not to cool the whole provider
    down; the contract says the slot is released and admission decides."""
    world = make_world(tmp_path, monkeypatch, max_concurrent=1)
    try:
        world.rl.queue(rl(retry_after=60), {"gate": "b"})
        world.start_scheduler()
        a = world.simple("A", "rlagent")
        interrupted(world)
        b = world.simple("B", "rlagent", fx={"gate": "b"})
        world.wait_running(b, WAIT)
        is_pending(world, a)
        world.rl.open_gate("b")
        world.wait_state(b, "done", WAIT)
    finally:
        world.close()


# ======================================== reviewer verdict and loop rounds

def test_tb_r4_a_rate_limited_worker_does_not_advance_the_loop(w):
    w.fxw.queue(rl(retry_after=3), commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    c1 = interrupted(w, w.fxw)
    time.sleep(1.2)
    assert w.fxr.spawns() == 0, "the reviewer launched on a worker that was only rate limited"
    assert w.get(loop)["loop"]["rounds_rejected"] == 0
    assert w.get(wk)["state"] not in ("done", "cancelled")
    assert "round_rejected" not in w.transitions(loop)
    c2 = nth_call(w, 2, w.fxw, node=loop)
    assert c2["resume"] == c1["session"]
    done = w.wait_state(loop, "done", LONG_WAIT)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 1
    assert w.get(wk)["outcome"] == "completed"


def test_tb_r4_a_rate_limited_reviewer_settles_no_verdict_and_the_resume_decides(w):
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(rl(retry_after=2), verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    c1 = interrupted(w, w.fxr)
    w.quiet(1.2)
    node = w.get(rv)
    assert node["state"] not in ("done", "cancelled", "held"), (node["state"], node.get("hold"))
    assert node.get("outcome") is None
    assert not [g for g in w.get(loop)["generations"] if g.get("verdict")]
    assert w.get(loop)["loop"]["rounds_rejected"] == 0
    assert "failed" not in w.transitions(rv)
    c2 = nth_call(w, 2, w.fxr, node=loop)
    assert c2["resume"] == c1["session"]
    done = w.wait_state(loop, "done", LONG_WAIT)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert w.get(rv)["outcome"] == "approved"
    assert w.fxr.spawns() == 2 and w.fxw.spawns() == 1
    assert [r["replies"][-1].get("ok") for r in w.fxr.verdicts()] == [True]


# ================================================================ undo paths

def test_tb_r4_cancelling_the_node_cancels_the_pending_resume(w):
    w.rl.queue(rl(retry_after=2), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    interrupted(w)
    w.quiet(1.5)                                 # a tick: let the scheduler judge the run
    is_pending(w, node)
    reply = w.cancel(node)
    assert reply.get("ok"), reply
    assert w.get(node)["state"] == "cancelled"
    # past the Retry-After, and across a scheduler restart (a journaled resume
    # must not come back to life)
    w.restart_scheduler()
    w.quiet(3.0)
    assert w.rl.spawns() == 1, "a cancelled node was resumed"
    assert w.get(node)["state"] == "cancelled"


def test_tb_r4_cancelling_twice_while_pending_is_harmless(w):
    w.rl.queue(rl(retry_after=60), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    interrupted(w)
    w.quiet(1.5)
    is_pending(w, node)
    assert w.cancel(node).get("ok")
    again = w.cancel(node)                       # final already: refused or idempotent
    assert w.get(node)["state"] == "cancelled"
    assert w.rl.spawns() == 1
    assert isinstance(again, dict)


# ================================================================= restarts

@pytest.mark.parametrize("how", ["stop", "kill9"])
def test_tb_r4_a_pending_resume_survives_a_scheduler_restart_and_keeps_its_wait(w, how):
    w.rl.queue(rl(retry_after=5), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    if how == "kill9":
        w.kill9()
        w.start_scheduler()
    else:
        w.restart_scheduler()
    is_pending(w, node)
    assert w.rl.spawns() == 1, "resumed at once after the restart, ignoring the Retry-After"
    c2 = nth_call(w, 2, node=node)
    assert c2["t"] - c1["t"] >= 5.0, c2["t"] - c1["t"]
    assert c2["resume"] == c1["session"]
    assert w.wait_state(node, "done", WAIT)["outcome"] != "failed"
    assert w.rl.spawns() == 2


def test_tb_r4_a_resume_that_fell_due_while_the_scheduler_was_down_runs_at_start(w):
    w.rl.queue(rl(retry_after=2), {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    w.kill9()
    time.sleep(max(0.0, c1["t"] + 3.0 - time.time()))
    assert w.rl.spawns() == 1
    w.start_scheduler()
    c2 = nth_call(w, 2, timeout=WAIT, node=node)
    assert c2["resume"] == c1["session"]
    assert w.wait_state(node, "done", WAIT)["outcome"] != "failed"
    assert w.rl.spawns() == 2, "the resume ran more than once"


# ============================================================ not resumable

def test_tb_r4_when_the_session_cannot_be_resumed_the_node_relaunches_fresh_and_reads_the_old_run(w):
    w.rl.queue(rl(retry_after=1), {"resume_fails": True}, {})
    w.start_scheduler()
    node = w.simple("A", "rlagent")
    c1 = interrupted(w)
    old_run = w.get(node)["runs"][0]["run_id"]
    worktree = w.tree_nodes()[old_run].get("worktree", "")
    c2 = nth_call(w, 2, node=node)
    assert c2["resume"] == c1["session"], "the resume was not tried first"
    c3 = nth_call(w, 3, timeout=LONG_WAIT, node=node)
    assert c3["resume"] is None, "the fallback must be a fresh launch"
    assert old_run in c3["prompt"] or (worktree and worktree in c3["prompt"]), (
        "the fresh run is not told where the old run's directory is")
    done = w.wait_state(node, "done", WAIT)
    assert done["outcome"] != "failed"
    assert "failed" not in w.transitions(node)
