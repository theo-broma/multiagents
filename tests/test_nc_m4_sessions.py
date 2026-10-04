"""M4 — session aliases of a loop: NC-R31 (activation, stable worktree),
NC-R62 (alias worktree ownership, dirty hold), NC-R32 (model change, new
session). The NC-R30 alias tests of `tests/test_nc_m2_sessions.py` are M4
acceptance too (NC-R81) and are not repeated here.

NC-R32's codex claim (`session_model_change: true` keeps the session across a
model change) is verified against the real CLI by
`test_nc_r32_codex_keeps_the_session_across_a_model_change`, skipped unless
`MULTIAGENTS_TEST_REAL_CODEX=1` (and `MULTIAGENTS_TEST_CODEX_MODELS=<a>,<b>`
naming two models the logged-in codex accepts).

Assumptions where the contract is silent (kept loose):
- the activation prompt states the working directory (NC-R31).
- a refused model change is `ok: false` and leaves the node `held` untouched.
- the `dirty_worktree` hold sits on the node whose activation was refused (the
  reviewer); the diff path is somewhere in `hold.detail`; the leftover file is
  still on disk.
- NOT TESTED (cannot be forced from outside): a lost provider session
  (`session_lost`), the fixture cannot make a resume "unresumable".
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, err_code, finding, verdict_entry  # noqa: E402

PAIR = """\
template: m4pair
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: wrk, kind: simple, agent: wk, task: "implement the thing"}
    - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
"""


def make(tmp_path, monkeypatch, **reviewer_provider):
    w = M4World(tmp_path, monkeypatch)
    w.fxw = w.provider("fxw")
    w.fxr = w.provider("fxr", **reviewer_provider)
    w.agent("wk", "fxw", writes=True)
    w.agent("rv", "fxr", writes=True)
    return w


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def wchange(tmp_path, monkeypatch):
    world = make(tmp_path, monkeypatch, session_model_change=True)
    yield world
    world.close()


def start(w, rounds=3):
    w.start_scheduler()
    w.register_ok(PAIR)
    top = w.instantiate_ok("m4pair", {"rounds": rounds})
    wk, rv = w.children(top)
    return top, wk, rv


# ----------------------------------------------------------------- NC-R31

def test_nc_r31_the_second_activation_resumes_the_session_at_the_same_path_at_the_new_commit(w):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"),
                {"write": {"b.txt": "two\n"}, "delete": ["a.txt"], "commit": "r2"})
    w.fxr.queue(verdict_entry("rejected", [finding("redo")]), verdict_entry("approved"))
    top, wk, rv = start(w)
    done = w.wait_state(top, "done", timeout=90)
    first, second = w.fxr.calls()
    assert second["resume"] == first["session"], "a new provider session was started"
    assert second["cwd"] == first["cwd"], "the alias did not keep one stable path"
    assert first["pid"] != second["pid"]
    assert first["cwd"] in first["prompt"] and second["head"] in second["prompt"]
    assert second["head"] != first["head"]
    assert "a.txt" in first["files"] and "a.txt" not in second["files"], "a file of the first activation is left"
    assert "b.txt" in second["files"]
    runs = {r["run_id"] for r in w.get(rv)["runs"]}
    assert len(runs) == 2 and len(w.get(rv)["runs"]) == 2, "each activation is its own run"


def test_nc_r31_the_activation_prompt_carries_the_nodes_task(w):
    w.fxw.queue(commit_entry("a.txt", "one\n"))
    w.fxr.queue(verdict_entry("approved"))
    top, wk, rv = start(w)
    w.wait_state(top, "done", timeout=60)
    assert "review the thing" in w.fxr.calls()[0]["prompt"]


# ----------------------------------------------------------------- NC-R62

def test_nc_r62_a_dirty_alias_checkout_holds_the_next_activation_and_deletes_nothing(w):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"), commit_entry("b.txt", "two\n", "r2"))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], leave={"scratch.txt": "left behind\n"}),
                verdict_entry("approved"))
    top, wk, rv = start(w)
    held = w.wait_held(rv, "dirty_worktree", timeout=90)
    assert w.fxr.spawns() == 1, "the reviewer was activated on a dirty checkout"
    leftover = Path(w.fxr.calls()[0]["cwd"]) / "scratch.txt"
    assert leftover.read_text() == "left behind\n", "the leftover file was deleted"
    assert "dirty_worktree" in json.dumps(held["hold"])
    w.quiet(2)
    assert w.fxr.spawns() == 1 and w.get(rv)["state"] == "held"


# ----------------------------------------------------------------- NC-R32

def reject_to_max(w):
    w.fxw.queue(commit_entry("a.txt", "one\n"), commit_entry("b.txt", "two\n"))
    w.fxr.queue(verdict_entry("rejected", [finding("x")]), verdict_entry("approved"))
    top, wk, rv = start(w, rounds=1)
    w.wait_held(top, "loop_max", timeout=90)
    return top, wk, rv


def test_nc_r32_a_model_change_of_an_aliased_child_needs_new_session(w):
    top, wk, rv = reject_to_max(w)
    before = w.get(top)
    reply = w.root_op("relaunch_node", top, max_rounds=3, pins={rv: {"model": "fxr/m2"}})
    assert reply.get("ok") is False, reply
    after = w.get(top)
    assert after["state"] == "held" and after["revision"] == before["revision"]
    w.quiet(2)
    assert w.fxr.spawns() == 1 and w.fxw.spawns() == 1


def test_nc_r32_with_new_session_a_new_provider_session_is_bound_on_the_new_model(w):
    top, wk, rv = reject_to_max(w)
    reply = w.root_op("relaunch_node", top, max_rounds=3, pins={rv: {"model": "fxr/m2"}},
                      new_session=[rv])
    assert reply.get("ok") is True, reply
    assert w.wait_state(top, "done", timeout=90)["outcome"] == "approved"
    first, second = w.fxr.calls()
    assert second["resume"] is None and second["session"] != first["session"]
    assert second["model"] == "fxr/m2" and first["model"] == "fxr/m1"


def test_nc_r32_a_provider_declaring_session_model_change_resumes_the_session(wchange):
    w = wchange
    top, wk, rv = reject_to_max(w)
    reply = w.root_op("relaunch_node", top, max_rounds=3, pins={rv: {"model": "fxr/m2"}})
    assert reply.get("ok") is True, reply
    assert w.wait_state(top, "done", timeout=90)["outcome"] == "approved"
    first, second = w.fxr.calls()
    assert second["resume"] == first["session"] and second["model"] == "fxr/m2"


def test_nc_r32_a_non_aliased_child_changes_model_freely_and_the_alias_is_untouched(w):
    top, wk, rv = reject_to_max(w)
    assert w.root_op("relaunch_node", top, max_rounds=3, pins={wk: {"model": "fxw/m2"}}).get("ok") is True
    w.wait_state(top, "done", timeout=90)
    first, second = w.fxr.calls()
    assert second["resume"] == first["session"] and second["model"] == first["model"]
    assert [c["model"] for c in w.fxw.calls()] == ["fxw/m1", "fxw/m2"]


def test_nc_r32_new_session_naming_a_child_without_an_alias_or_unknown_is_invalid(w):
    top, wk, rv = reject_to_max(w)
    reply = w.root_op("relaunch_node", top, max_rounds=3, new_session=["nd-deadbeef"])
    assert err_code(reply) == "invalid", reply
    assert w.get(top)["state"] == "held"


@pytest.mark.skipif(os.environ.get("MULTIAGENTS_TEST_REAL_CODEX") != "1"
                    or shutil.which("codex") is None,
                    reason="verifies the provisional `session_model_change: true` of codex "
                           "against the real CLI; set MULTIAGENTS_TEST_REAL_CODEX=1 and "
                           "MULTIAGENTS_TEST_CODEX_MODELS=<a>,<b> (a logged-in `codex` on PATH)")
def test_nc_r32_codex_keeps_the_session_across_a_model_change(tmp_path, monkeypatch):
    """Two activations of one alias on a real codex agent, the second after a
    model change WITHOUT `new_session`: the product must accept it (codex
    declares `session_model_change: true`) and the second turn must run in the
    first turn's codex session. If this fails, the shipped default for codex
    must become false (NC-R32)."""
    import yaml
    m1, m2 = os.environ["MULTIAGENTS_TEST_CODEX_MODELS"].split(",")
    w = M4World(tmp_path, monkeypatch)
    shipped = yaml.safe_load((Path(__file__).resolve().parents[1] / "src/multiagents/defaults/providers.yaml").read_text())
    w.providers["fx"].entry = shipped["providers"]["codex"]
    w.fxr = w.providers["fx"]
    w.agent("wk", "fx", writes=True)
    w.agents["wk"]["model"] = m1
    w.agent("rv", "fx", writes=True)
    w.agents["rv"]["model"] = m1
    try:
        w.start_scheduler()
        w.register_ok(PAIR.replace("implement the thing", "Create hello.txt containing hi, commit it.")
                      .replace("review the thing", "Review the commit. Remember the word PINEAPPLE."))
        top = w.instantiate_ok("m4pair", {"rounds": 1})
        wk, rv = w.children(top)
        w.until(lambda: w.get(rv)["runs"], timeout=300, what="the first reviewer activation")
        w.until(lambda: w.get(top)["state"] in ("held", "done"), timeout=600, what="the first round")
        first_session = w.get(rv)["runs"][0]
        if w.get(top)["state"] == "done":
            pytest.skip("the real reviewer approved at once; no second activation to compare")
        reply = w.root_op("relaunch_node", top, max_rounds=3, pins={rv: {"model": m2}})
        assert reply.get("ok") is True, reply
        w.wait_state(top, "done", timeout=600)
        runs = w.get(rv)["runs"]
        assert len(runs) == 2
        tree = w.tree_nodes()
        s1 = tree[runs[0]["run_id"]].get("session_id")
        s2 = tree[runs[1]["run_id"]].get("session_id")
        assert s1 and s1 == s2, "codex did not keep the session across the model change"
    finally:
        w.close()
